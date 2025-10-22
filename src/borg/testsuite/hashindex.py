# Note: these tests are part of the self test, do not use or import pytest functionality here.
#       See borg.selftest for details. If you add/remove test methods, update SELFTEST_COUNT

import base64
import hashlib
import io
import os
import tempfile
import zlib

from ..hashindex import NSIndex, ChunkIndex
from ..crypto.file_integrity import IntegrityCheckedFile, FileIntegrityError
from . import BaseTestCase, unopened_tempfile


def H(x):
    # make some 32byte long thing that depends on x
    return bytes("%-0.32d" % x, "ascii")


def H2(x):
    # like H(x), but with pseudo-random distribution of the output value
    return hashlib.sha256(H(x)).digest()


class HashIndexTestCase(BaseTestCase):
    def _generic_test(self, cls, make_value, sha):
        idx = cls()
        self.assert_equal(len(idx), 0)
        # Test set
        for x in range(100):
            idx[H(x)] = make_value(x)
        self.assert_equal(len(idx), 100)
        for x in range(100):
            self.assert_equal(idx[H(x)], make_value(x))
        # Test update
        for x in range(100):
            idx[H(x)] = make_value(x * 2)
        self.assert_equal(len(idx), 100)
        for x in range(100):
            self.assert_equal(idx[H(x)], make_value(x * 2))
        # Test delete
        for x in range(50):
            del idx[H(x)]
        # Test some keys still in there
        for x in range(50, 100):
            assert H(x) in idx
        # Test some keys not there any more
        for x in range(50):
            assert H(x) not in idx
        # Test delete non-existing key
        for x in range(50):
            self.assert_raises(KeyError, idx.__delitem__, H(x))
        self.assert_equal(len(idx), 50)
        with unopened_tempfile() as filepath:
            idx.write(filepath)
            del idx
            # Verify file contents
            with open(filepath, "rb") as fd:
                self.assert_equal(hashlib.sha256(fd.read()).hexdigest(), sha)
            # Make sure we can open the file
            idx = cls.read(filepath)
            self.assert_equal(len(idx), 50)
            for x in range(50, 100):
                self.assert_equal(idx[H(x)], make_value(x * 2))
            idx.clear()
            self.assert_equal(len(idx), 0)
            idx.write(filepath)
            del idx
            self.assert_equal(len(cls.read(filepath)), 0)
        idx = cls()
        # Test setdefault - set non-existing key
        idx.setdefault(H(0), make_value(42))
        assert H(0) in idx
        assert idx[H(0)] == make_value(42)
        # Test setdefault - do not set existing key
        idx.setdefault(H(0), make_value(23))
        assert H(0) in idx
        assert idx[H(0)] == make_value(42)
        # Test setdefault - get-like return value, key not present
        assert idx.setdefault(H(1), make_value(23)) == make_value(23)
        # Test setdefault - get-like return value, key present
        assert idx.setdefault(H(0), make_value(23)) == make_value(42)
        # clean up setdefault test
        del idx

    def test_nsindex(self):
        self._generic_test(
            NSIndex, lambda x: (x, x, x), "640b909cf07884cc11fdf5431ffc27dee399770ceadecce31dffecd130a311a3"
        )

    def test_chunkindex(self):
        self._generic_test(
            ChunkIndex, lambda x: (x, x), "5915fcf986da12e5f3ac68e05242b9c729e6101b0460b1d4e4a9e9f7cdf1b7da"
        )

    def test_resize(self):
        n = 2000  # Must be >= MIN_BUCKETS
        with unopened_tempfile() as filepath:
            idx = NSIndex()
            idx.write(filepath)
            initial_size = os.path.getsize(filepath)
            self.assert_equal(len(idx), 0)
            for x in range(n):
                idx[H(x)] = x, x, x
            idx.write(filepath)
            assert initial_size < os.path.getsize(filepath)
            for x in range(n):
                del idx[H(x)]
            self.assert_equal(len(idx), 0)
            idx.write(filepath)
            self.assert_equal(initial_size, os.path.getsize(filepath))

    def test_iteritems(self):
        idx = NSIndex()
        for x in range(100):
            idx[H(x)] = x, x, x
        iterator = idx.iteritems()
        all = list(iterator)
        self.assert_equal(len(all), 100)
        # iterator is already exhausted by list():
        self.assert_raises(StopIteration, next, iterator)
        second_half = list(idx.iteritems(marker=all[49][0]))
        self.assert_equal(len(second_half), 50)
        self.assert_equal(second_half, all[50:])


class HashIndexExtraTestCase(BaseTestCase):
    """These tests are separate because they should not become part of the selftest."""

    def test_chunk_indexer(self):
        # see _hashindex.c hash_sizes, we want to be close to the max. load
        # because interesting errors happen there.
        key_count = int(65537 * ChunkIndex.MAX_LOAD_FACTOR) - 10
        index = ChunkIndex(key_count)
        all_keys = [hashlib.sha256(H(k)).digest() for k in range(key_count)]
        # we're gonna delete 1/3 of all_keys, so let's split them 2/3 and 1/3:
        keys, to_delete_keys = all_keys[0 : (2 * key_count // 3)], all_keys[(2 * key_count // 3) :]

        for i, key in enumerate(keys):
            index[key] = (i, i)
        for i, key in enumerate(to_delete_keys):
            index[key] = (i, i)

        for key in to_delete_keys:
            del index[key]
        for i, key in enumerate(keys):
            assert index[key] == (i, i)
        for key in to_delete_keys:
            assert index.get(key) is None

        # now delete every key still in the index
        for key in keys:
            del index[key]
        # the index should now be empty
        assert list(index.iteritems()) == []


class HashIndexSizeTestCase(BaseTestCase):
    def test_size_on_disk(self):
        idx = ChunkIndex()
        assert idx.size() == 1024 + 1031 * (32 + 2 * 4)

    def test_size_on_disk_accurate(self):
        idx = ChunkIndex()
        for i in range(1234):
            idx[H(i)] = i, i**2
        with unopened_tempfile() as filepath:
            idx.write(filepath)
            size = os.path.getsize(filepath)
        assert idx.size() == size


class HashIndexRefcountingTestCase(BaseTestCase):
    def test_chunkindex_add(self):
        idx1 = ChunkIndex()
        idx1.add(H(1), 5, 6)
        assert idx1[H(1)] == (5, 6)
        idx1.add(H(1), 1, 2)
        assert idx1[H(1)] == (6, 2)

    def test_setitem_raises(self):
        idx1 = ChunkIndex()
        with self.assert_raises(AssertionError):
            idx1[H(1)] = ChunkIndex.MAX_VALUE + 1, 0

    def test_keyerror(self):
        idx = ChunkIndex()
        with self.assert_raises(KeyError):
            idx[H(1)]
        with self.assert_raises(OverflowError):
            idx.add(H(1), -1, 0)


class HashIndexDataTestCase(BaseTestCase):
    # This bytestring was created with borg2-pre 2022-09-30
    HASHINDEX = (
        b"eJzt0DEKgwAMQNFoBXsMj9DqDUQoToKTR3Hzwr2DZi+0HS19HwIZHhnST/OjHYeljIhLTl1FVDlN7te"
        b"Q9M/tGcdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHMdxHM"
        b"dxHMdxHMdxHMdxHMdxHMdxHPfqbu+7F2nKz67Nc9sX97r1+Rt/4TiO4ziO4ziO4ziO4ziO4ziO4ziO4"
        b"ziO4ziO4ziO4ziO4ziO4ziO4ziO4ziO487lDoRvHEk="
    )

    def _serialize_hashindex(self, idx):
        with tempfile.TemporaryDirectory() as tempdir:
            file = os.path.join(tempdir, "idx")
            idx.write(file)
            with open(file, "rb") as f:
                return self._pack(f.read())

    def _deserialize_hashindex(self, bytestring):
        with tempfile.TemporaryDirectory() as tempdir:
            file = os.path.join(tempdir, "idx")
            with open(file, "wb") as f:
                f.write(self._unpack(bytestring))
            return ChunkIndex.read(file)

    def _pack(self, bytestring):
        return base64.b64encode(zlib.compress(bytestring))

    def _unpack(self, bytestring):
        return zlib.decompress(base64.b64decode(bytestring))

    def test_identical_creation(self):
        idx1 = ChunkIndex()
        idx1[H(1)] = 1, 2
        idx1[H(2)] = 2**31 - 1, 0
        idx1[H(3)] = 4294962296, 0  # 4294962296 is -5000 interpreted as an uint32_t

        serialized = self._serialize_hashindex(idx1)
        assert self._unpack(serialized) == self._unpack(self.HASHINDEX)


class HashIndexIntegrityTestCase(HashIndexDataTestCase):
    def write_integrity_checked_index(self, tempdir):
        idx = self._deserialize_hashindex(self.HASHINDEX)
        file = os.path.join(tempdir, "idx")
        with IntegrityCheckedFile(path=file, write=True) as fd:
            idx.write(fd)
        integrity_data = fd.integrity_data
        assert "final" in integrity_data
        assert "HashHeader" in integrity_data
        return file, integrity_data

    def test_integrity_checked_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            file, integrity_data = self.write_integrity_checked_index(tempdir)
            with open(file, "r+b") as fd:
                fd.write(b"Foo")
            with self.assert_raises(FileIntegrityError):
                with IntegrityCheckedFile(path=file, write=False, integrity_data=integrity_data) as fd:
                    ChunkIndex.read(fd)


class HashIndexCompactTestCase(HashIndexDataTestCase):
    def index(self, num_entries, num_buckets, num_empty):
        index_data = io.BytesIO()
        index_data.write(b"BORG2IDX")
        # version
        index_data.write((2).to_bytes(4, "little"))
        # num_entries
        index_data.write(num_entries.to_bytes(4, "little"))
        # num_buckets
        index_data.write(num_buckets.to_bytes(4, "little"))
        # num_empty
        index_data.write(num_empty.to_bytes(4, "little"))
        # key_size
        index_data.write((32).to_bytes(4, "little"))
        # value_size
        index_data.write((3 * 4).to_bytes(4, "little"))
        # reserved
        index_data.write(bytes(1024 - 32))

        self.index_data = index_data

    def index_from_data(self):
        self.index_data.seek(0)
        # Since we are trying to carefully control the layout of the hashindex,
        # we set permit_compact to prevent hashindex_read from resizing the hash table.
        index = ChunkIndex.read(self.index_data, permit_compact=True)
        return index

    def write_entry(self, key, *values):
        self.index_data.write(key)
        for value in values:
            self.index_data.write(value.to_bytes(4, "little"))

    def write_empty(self, key):
        self.write_entry(key, 0xFFFFFFFF, 0, 0)

    def write_deleted(self, key):
        self.write_entry(key, 0xFFFFFFFE, 0, 0)

    def compare_indexes(self, idx1, idx2):
        """Check that the two hash tables contain the same data.  idx1
        is allowed to have "mis-filed" entries, because we only need to
        iterate over it.  But idx2 needs to support lookup."""
        for k, v in idx1.iteritems():
            assert v == idx2[k]
        assert len(idx1) == len(idx2)

    def compare_compact(self, layout):
        """A generic test of a hashindex with the specified layout.  layout should
        be a string consisting only of the characters '*' (filled), 'D' (deleted)
        and 'E' (empty).
        """
        num_buckets = len(layout)
        num_empty = layout.count("E")
        num_entries = layout.count("*")
        self.index(num_entries=num_entries, num_buckets=num_buckets, num_empty=num_empty)
        k = 0
        for c in layout:
            if c == "D":
                self.write_deleted(H2(k))
            elif c == "E":
                self.write_empty(H2(k))
            else:
                assert c == "*"
                self.write_entry(H2(k), 3 * k + 1, 3 * k + 2, 3 * k + 3)
            k += 1
        idx = self.index_from_data()
        cpt = self.index_from_data()
        cpt.compact()
        # Note that idx is not a valid hash table, since the entries are not
        # stored where they should be.  So lookups of the form idx[k] can fail.
        # But cpt is a valid hash table, since there are no empty buckets.
        assert idx.size() == 1024 + num_buckets * (32 + 3 * 4)
        assert cpt.size() == 1024 + num_entries * (32 + 3 * 4)
        self.compare_indexes(idx, cpt)

    def test_simple(self):
        self.compare_compact("*DE**E")

    def test_first_empty(self):
        self.compare_compact("D*E**E")

    def test_last_used(self):
        self.compare_compact("D*E*E*")

    def test_too_few_empty_slots(self):
        self.compare_compact("D**EE*")

    def test_empty(self):
        self.compare_compact("DEDEED")

    def test_num_buckets_zero(self):
        self.compare_compact("")

    def test_already_compact(self):
        self.compare_compact("***")

    def test_all_at_front(self):
        self.compare_compact("*DEEED")
        self.compare_compact("**DEED")
        self.compare_compact("***EED")
        self.compare_compact("****ED")
        self.compare_compact("*****D")

    def test_all_at_back(self):
        self.compare_compact("EDEEE*")
        self.compare_compact("DEDE**")
        self.compare_compact("DED***")
        self.compare_compact("ED****")
        self.compare_compact("D*****")


class NSIndexTestCase(BaseTestCase):
    def test_nsindex_segment_limit(self):
        idx = NSIndex()
        with self.assert_raises(AssertionError):
            idx[H(1)] = NSIndex.MAX_VALUE + 1, 0, 0
        assert H(1) not in idx
        idx[H(2)] = NSIndex.MAX_VALUE, 0, 0
        assert H(2) in idx


class AllIndexTestCase(BaseTestCase):
    def test_max_load_factor(self):
        assert NSIndex.MAX_LOAD_FACTOR < 1.0
        assert ChunkIndex.MAX_LOAD_FACTOR < 1.0


class IndexCorruptionTestCase(BaseTestCase):
    def test_bug_4829(self):
        from struct import pack

        def HH(x, y, z):
            # make some 32byte long thing that depends on x, y, z.
            # same x will mean a collision in the hashtable as bucket index is computed from
            # first 4 bytes. giving a specific x targets bucket index x.
            # y is to create different keys and does not go into the bucket index calculation.
            # so, same x + different y --> collision
            return pack("<IIIIIIII", x, y, z, 0, 0, 0, 0, 0)  # 8 * 4 == 32

        idx = NSIndex()

        # create lots of colliding entries
        for y in range(700):  # stay below max load not to trigger resize
            idx[HH(0, y, 0)] = (0, y, 0)

        assert idx.size() == 1024 + 1031 * 44  # header + 1031 buckets

        # delete lots of the collisions, creating lots of tombstones
        for y in range(400):  # stay above min load not to trigger resize
            del idx[HH(0, y, 0)]

        # create lots of colliding entries, within the not yet used part of the hashtable
        for y in range(330):  # stay below max load not to trigger resize
            # at y == 259 a resize will happen due to going beyond max EFFECTIVE load
            # if the bug is present, that element will be inserted at the wrong place.
            # and because it will be at the wrong place, it can not be found again.
            idx[HH(600, y, 0)] = 600, y, 0

        # now check if hashtable contents is as expected:

        assert [idx.get(HH(0, y, 0)) for y in range(400, 700)] == [(0, y, 0) for y in range(400, 700)]

        assert [HH(0, y, 0) in idx for y in range(400)] == [False for y in range(400)]  # deleted entries

        # this will fail at HH(600, 259) if the bug is present.
        assert [idx.get(HH(600, y, 0)) for y in range(330)] == [(600, y, 0) for y in range(330)]
