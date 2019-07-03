##############################################################################
#
# Copyright (c) 2019 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""
Implements restoring from one database to another.

"""
from __future__ import absolute_import
from __future__ import print_function

from ZODB.POSException import StorageTransactionError

from ZODB.utils import u64 as bytes8_to_int64

from .vote import DatabaseLockedForTid

class Restore(object):
    """
    A type of begin state that wraps another begin state and adds the methods needed to
    restore or commit to a particular tid.
    """

    # batcher: An object that accumulates store operations
    # so they can be executed in batch (to minimize latency).
    batcher = None

    # _batcher_row_limit: The number of rows to queue before
    # calling the database.
    batcher_row_limit = 100

    # Methods from the underlying begin implementation we need to
    # expose.
    _COPY_ATTRS = (
        'store',
        'checkCurrentSerialInTransaction',
        'deletObject',
        'undo',
        'tpc_vote',
        'tpc_abort',
        'no_longer_stale',
    )

    def __init__(self, begin_state, committing_tid, status):
        # This is an extension we use for copyTransactionsFrom;
        # it is not part of the IStorage API.
        assert committing_tid is not None
        self.wrapping = begin_state

        # hold the commit lock and add the transaction now.
        # This will prevent anyone else from modifying rows
        # other than this transaction. We currently avoid the temp tables,
        # though, so if we do multiple things in a restore transaction,
        # we could still wind up with locking issues (I think?)
        storage = begin_state.storage
        adapter = storage._adapter
        cursor = storage._store_cursor
        packed = (status == 'p')
        try:
            committing_tid_lock = DatabaseLockedForTid.lock_database_for_given_tid(
                committing_tid, packed,
                cursor, adapter, begin_state.ude
            )
        except:
            storage._drop_store_connection()
            raise

        # This is now only used for restore()
        self.batcher = adapter.mover.make_batcher(
            storage._store_cursor,
            self.batcher_row_limit)

        for name in self._COPY_ATTRS:
            try:
                meth = getattr(begin_state, name)
            except AttributeError:
                continue
            else:
                setattr(self, name, meth)

        # Arrange for voting to store our batch too, since
        # the mover is unaware of it.
        # XXX: This isn't especially pretty.
        orig_factory = begin_state._tpc_vote_factory
        def tpc_vote_factory(state):
            vote_state = orig_factory(state)
            vote_state.committing_tid_lock = committing_tid_lock

            orig_flush = vote_state._flush_temps_to_db
            def flush(cursor):
                orig_flush(cursor)
                self.batcher.flush()
            vote_state._flush_temps_to_db = flush
            return vote_state

        begin_state._tpc_vote_factory = tpc_vote_factory

    def restore(self, oid, this_tid, data, prev_txn, transaction):
        # Similar to store() (see comments in FileStorage.restore for
        # some differences), but used for importing transactions.
        # Note that *data* can be None.
        # The *prev_txn* "backpointer" optimization/hint is ignored.
        #
        # pylint:disable=unused-argument
        state = self.wrapping
        if transaction is not state.transaction:
            raise StorageTransactionError(self, transaction)

        adapter = state.storage._adapter
        cursor = state.storage._store_cursor
        assert cursor is not None
        oid_int = bytes8_to_int64(oid)
        tid_int = bytes8_to_int64(this_tid)

        with state.storage._lock:
            state.max_stored_oid = max(state.max_stored_oid, oid_int)
            # Save the `data`.  Note that `data` can be None.
            # Note also that this doesn't go through the cache.

            # TODO: Make it go through the cache, or at least the same
            # sort of queing thing, so that we can do a bulk COPY?
            # This complicates restoreBlob() and it complicates voting.
            adapter.mover.restore(
                cursor, self.batcher, oid_int, tid_int, data)

    def restoreBlob(self, oid, serial, data, blobfilename, prev_txn, txn):
        self.restore(oid, serial, data, prev_txn, txn)
        state = self.wrapping
        with state.storage._lock:
            # Restoring the entry for the blob MAY have used the batcher, and
            # we're going to want to foreign-key off of that data when
            # we add blob chunks (since we skip the temp tables).
            # Ideally, we wouldn't need to flush the batcher here
            # (we'd prefer having DEFERRABLE INITIALLY DEFERRED FK
            # constraints, but as-of 8.0 MySQL doesn't support that.)
            self.batcher.flush()
            cursor = state.storage._store_cursor
            state.storage.blobhelper.restoreBlob(cursor, oid, serial, blobfilename)
