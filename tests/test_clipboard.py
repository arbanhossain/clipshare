import io
import unittest

from clipshare.clipboard import Clipboard, pick_image_target, to_png

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def fake_clipboard(offers, wayland=False, xclip=None, max_image_bytes=1024 * 1024):
    """A Clipboard whose helper binaries are replaced by a canned offer table.

    `offers` maps target/MIME name to the bytes that tool would return.
    """
    cb = Clipboard(None, max_image_bytes)
    cb.wayland = wayland
    cb.xclip = xclip
    cb.reads = []
    cb.writes = []

    def fake_read(cmd):
        cb.reads.append(cmd)
        if wayland:
            if "--list-types" in cmd:
                return "\n".join(offers).encode()
            target = cmd[-1]
        else:
            target = cmd[cmd.index("-t") + 1]
            if target == "TARGETS":
                return "\n".join(offers).encode()
        return offers.get(target)

    def fake_write(cmd, data):
        cb.writes.append((cmd, data))
        return True

    cb._read = fake_read
    cb._write = fake_write
    return cb


class TestTargetSelection(unittest.TestCase):
    def test_prefers_png_over_other_image_targets(self):
        self.assertEqual(
            pick_image_target(["TARGETS", "image/jpeg", "image/png"]), "image/png"
        )

    def test_falls_back_to_any_image_target(self):
        self.assertEqual(pick_image_target(["TARGETS", "image/x-foo"]), "image/x-foo")

    def test_no_image_target(self):
        self.assertIsNone(pick_image_target(["TARGETS", "text/plain", "STRING"]))


class TestWaylandCapture(unittest.TestCase):
    def test_image_is_captured(self):
        cb = fake_clipboard({"image/png": PNG}, wayland=True)
        clip = cb.get()
        self.assertEqual(clip.kind, "image")
        self.assertEqual(clip.payload, PNG)
        self.assertTrue(clip.digest)

    def test_text_wins_when_both_are_offered(self):
        cb = fake_clipboard(
            {"text/plain": b"hello", "image/png": PNG}, wayland=True
        )
        clip = cb.get()
        self.assertEqual(clip.kind, "text")
        self.assertEqual(clip.payload, "hello")

    def test_binary_payload_is_never_decoded_as_text(self):
        # Regression: wl-paste used to be read as UTF-8 before the type was
        # known, so image bytes raised UnicodeDecodeError and the image was lost.
        cb = fake_clipboard({"text/plain": PNG}, wayland=True)
        self.assertEqual(cb.get().kind, "none")

    def test_empty_clipboard(self):
        self.assertEqual(fake_clipboard({}, wayland=True).get().kind, "none")


class TestX11Capture(unittest.TestCase):
    def test_image_is_captured_via_xclip(self):
        cb = fake_clipboard({"image/png": PNG}, xclip="/usr/bin/xclip")
        clip = cb.get()
        self.assertEqual(clip.kind, "image")
        self.assertEqual(clip.payload, PNG)

    def test_no_image_support_without_a_helper(self):
        cb = fake_clipboard({"image/png": PNG})
        self.assertEqual(cb.get().kind, "none")
        self.assertIsNone(cb.image_tool)
        self.assertIn("xclip", cb.image_help())


class TestImageNormalisation(unittest.TestCase):
    def test_png_passes_through_unchanged(self):
        self.assertIs(to_png(PNG, "image/png"), PNG)

    def test_jpeg_is_converted_to_png(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="JPEG")
        cb = fake_clipboard({"image/jpeg": buf.getvalue()}, wayland=True)
        clip = cb.get()
        self.assertEqual(clip.kind, "image")
        self.assertTrue(clip.payload.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_undecodable_image_is_dropped(self):
        cb = fake_clipboard({"image/jpeg": b"not an image"}, wayland=True)
        self.assertEqual(cb.get().kind, "none")

    def test_oversized_image_is_dropped(self):
        big = b"\x89PNG\r\n\x1a\n" + b"0" * 4096
        cb = fake_clipboard({"image/png": big}, wayland=True, max_image_bytes=1024)
        self.assertEqual(cb.get().kind, "none")


class TestSetImage(unittest.TestCase):
    def test_wayland_write(self):
        cb = fake_clipboard({}, wayland=True)
        cb.set_image(PNG)
        cmd, data = cb.writes[0]
        self.assertEqual(cmd[:1], ["wl-copy"])
        self.assertIn("image/png", cmd)
        self.assertEqual(data, PNG)

    def test_x11_write(self):
        cb = fake_clipboard({}, xclip="/usr/bin/xclip")
        cb.set_image(PNG)
        cmd, data = cb.writes[0]
        self.assertEqual(cmd[0], "/usr/bin/xclip")
        self.assertIn("image/png", cmd)
        self.assertEqual(data, PNG)

    def test_missing_helper_explains_what_to_install(self):
        cb = fake_clipboard({})
        with self.assertRaises(RuntimeError) as ctx:
            cb.set_image(PNG)
        self.assertIn("install", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
