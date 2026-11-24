"""Ranged reads, offset/append/mode writes and big files in parts (CloudFiles)."""

import asyncio
import io
import os
import re
import tempfile
import unittest
import urllib.parse

from kosa8 import Kosa8Cloud, Kosa8Error
from kosa8.aio import AsyncCloud, AsyncCloudSandbox
from kosa8.cloud import CloudSandbox

KEY = "k8c_live_testkey"
SBX = {"id": "sbx_1", "state": "running", "template": "base"}


class FakeFiles:
    """A sandbox file served the way /v1/sandboxes/{id}/files answers."""

    def __init__(self, data=b"", ranges=True, fail_first=None):
        self.data = bytearray(data)
        self.ranges = ranges
        self.calls = []
        self.fail_first = fail_first  # the status of one failed first answer

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((method, u.path, q, dict(headers)))
        if self.fail_first is not None:
            st, self.fail_first = self.fail_first, None
            return st, b'{"error":"host_failed","message":"boom"}', {}
        if method == "GET":
            rng = headers.get("Range")
            if rng and self.ranges:
                m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
                a = int(m.group(1))
                b = int(m.group(2)) if m.group(2) else len(self.data) - 1
                if a >= len(self.data):
                    return 416, b'{"error":"range_not_satisfiable","message":"past the end"}', {}
                return 206, bytes(self.data[a:b + 1]), {}
            return 200, bytes(self.data), {}
        if method == "PUT":
            if "offset" in q:
                off = int(q["offset"])
                if len(self.data) < off:
                    self.data.extend(b"\0" * (off - len(self.data)))
                self.data[off:off + len(body)] = body
            elif q.get("append") == "1":
                self.data.extend(body)
            else:
                self.data = bytearray(body)
            return 200, b'{"file":{"path":"%s","size":%d}}' % (q["path"].encode(), len(self.data)), {}
        raise AssertionError(method)

    async def aopen(self, *a):
        return self(*a)


def box(fake):
    return CloudSandbox(Kosa8Cloud(KEY, env={}, opener=fake), dict(SBX))


class SyncFiles(unittest.TestCase):
    def test_ranged_read_sends_range(self):
        fake = FakeFiles(b"0123456789")
        self.assertEqual(box(fake).files.read("/workspace/f", offset=2, length=3), b"234")
        self.assertEqual(fake.calls[-1][3]["Range"], "bytes=2-4")
        self.assertEqual(box(fake).files.read("/workspace/f", offset=7), b"789")
        self.assertEqual(fake.calls[-1][3]["Range"], "bytes=7-")
        self.assertEqual(box(fake).files.read("/workspace/f"), b"0123456789")
        self.assertNotIn("Range", fake.calls[-1][3])

    def test_ranged_read_on_a_host_without_ranges_is_sliced(self):
        fake = FakeFiles(b"0123456789", ranges=False)
        self.assertEqual(box(fake).files.read("/workspace/f", offset=2, length=3), b"234")
        self.assertEqual(box(fake).files.read("/workspace/f", offset=8), b"89")

    def test_write_query_flags(self):
        fake = FakeFiles(b"abc")
        f = box(fake).files
        f.write("/workspace/f", b"Z", offset=1)
        self.assertEqual(bytes(fake.data), b"aZc")
        f.write("/workspace/f", b"!", append=True)
        self.assertEqual(bytes(fake.data), b"aZc!")
        f.write("/workspace/run.sh", "echo", mode=0o755)
        self.assertEqual(fake.calls[-1][2]["mode"], "755")
        with self.assertRaises(ValueError):
            f.write("/workspace/f", b"x", offset=1, append=True)
        with self.assertRaises(ValueError):
            f.write("/workspace/f", b"x", mode="999")
        with self.assertRaises(ValueError):
            f.read("/workspace/f", offset=-1)

    def test_append_is_never_resent(self):
        fake = FakeFiles(b"abc", fail_first=503)
        with self.assertRaises(Kosa8Error):
            box(fake).files.write("/workspace/f", b"!", append=True)
        self.assertEqual(len(fake.calls), 1)
        fake = FakeFiles(b"abc", fail_first=503)
        box(fake).files.write("/workspace/f", b"!", offset=3)  # an offset write is safe to resend
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(bytes(fake.data), b"abc!")

    def test_upload_and_download_in_parts(self):
        payload = os.urandom(10_000)
        fake = FakeFiles()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.bin")
            with open(src, "wb") as fh:
                fh.write(payload)
            info = box(fake).files.upload("/workspace/big.bin", src, part_size=4096, mode="644")
            self.assertEqual(bytes(fake.data), payload)
            self.assertEqual(info["size"], len(payload))
            puts = [c for c in fake.calls if c[0] == "PUT"]
            self.assertEqual([c[2].get("offset") for c in puts], [None, "4096", "8192"])
            self.assertEqual(puts[0][2]["mode"], "644")
            dst = os.path.join(d, "out.bin")
            n = box(fake).files.download("/workspace/big.bin", dst, part_size=4096)
            self.assertEqual(n, len(payload))
            with open(dst, "rb") as fh:
                self.assertEqual(fh.read(), payload)

    def test_download_exact_multiple_and_empty(self):
        fake = FakeFiles(b"x" * 8192)
        out = io.BytesIO()
        self.assertEqual(box(fake).files.download("/workspace/f", out, part_size=4096), 8192)
        self.assertEqual(out.getvalue(), b"x" * 8192)
        fake = FakeFiles(b"", ranges=False)
        out = io.BytesIO()
        self.assertEqual(box(fake).files.download("/workspace/f", out, part_size=4096), 0)

    def test_upload_empty_and_exact_multiple(self):
        fake = FakeFiles(b"old")
        box(fake).files.upload("/workspace/f", io.BytesIO(b""), part_size=4)
        self.assertEqual(bytes(fake.data), b"")
        fake = FakeFiles()
        box(fake).files.upload("/workspace/f", io.BytesIO(b"abcdefgh"), part_size=4)
        self.assertEqual(bytes(fake.data), b"abcdefgh")
        self.assertEqual(len([c for c in fake.calls if c[0] == "PUT"]), 2)

    def test_download_from_host_without_ranges(self):
        fake = FakeFiles(b"hello world", ranges=False)
        out = io.BytesIO()
        self.assertEqual(box(fake).files.download("/workspace/f", out, part_size=4), 11)
        self.assertEqual(out.getvalue(), b"hello world")


class AsyncFiles(unittest.TestCase):
    def test_async_parts_and_range(self):
        payload = os.urandom(5000)
        fake = FakeFiles()

        async def go():
            async with AsyncCloud(KEY, env={}, async_opener=fake.aopen) as cloud:
                b = AsyncCloudSandbox(cloud, dict(SBX))
                await b.files.upload("/workspace/f", io.BytesIO(payload), part_size=2048)
                out = io.BytesIO()
                n = await b.files.download("/workspace/f", out, part_size=2048)
                part = await b.files.read("/workspace/f", offset=10, length=5)
                return n, out.getvalue(), part

        n, got, part = asyncio.run(go())
        self.assertEqual((n, got, part), (5000, payload, payload[10:15]))


if __name__ == "__main__":
    unittest.main()
