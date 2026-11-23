import json
import os
import tempfile
import unittest

from _t import ROOT, E

from ofh.emu import profile


class Profiles(unittest.TestCase):
    def test_template_loads_and_is_empty(self):
        p = profile.load(os.path.join(ROOT, "profiles", "template.json"))
        self.assertIsNone(p.base)
        self.assertEqual(p.ret_kind, {})
        self.assertEqual(p.dis_command, [])
        self.assertEqual(p.missing(), ["image.base"])

    def test_template_holds_no_value(self):
        with open(os.path.join(ROOT, "profiles", "template.json")) as f:
            raw = json.load(f)
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k != "_comment" and k != "isa":
                        walk(v)
            elif isinstance(x, list):
                self.assertEqual(x, [])
            else:
                self.assertIn(x, (None, ""), "the shipped template must be empty, found %r" % (x,))
        walk(raw)

    def test_apply_lists_missing_fields(self):
        p = profile.Profile({})
        with self.assertRaises(profile.ProfileError) as cm:
            p.apply()
        self.assertIn("image.base", str(cm.exception))

    def test_unknown_keys_are_errors(self):
        for raw in ({"bse": 1}, {"image": {"bsae": 1}}, {"disassembler": {"cmd": []}}):
            with self.assertRaises(profile.ProfileError):
                profile.Profile(raw)

    def test_other_isa_refused(self):
        with self.assertRaises(profile.ProfileError):
            profile.Profile({"isa": "arm"})

    def test_apply_configures_core(self):
        p = profile.Profile({"image": {"base": "0x400000"}, "ret_kind": {"0x900000": "retf"}})
        p.apply()
        self.assertEqual((E.BASE, E.IMG_LO), (0x400000, 0x400000))
        self.assertEqual(E.RET_KIND, {0x900000: "retf"})
        E.RET_KIND.clear()

    def test_bad_ret_kind(self):
        with self.assertRaises(profile.ProfileError):
            profile.Profile({"ret_kind": {"0x10": "jump"}})

    def test_relative_image_path(self):
        with tempfile.TemporaryDirectory() as d:
            fn = os.path.join(d, "p.json")
            with open(fn, "w") as f:
                json.dump({"image": {"base": 1, "file": "img.bin"}}, f)
            self.assertEqual(profile.load(fn).image_file, os.path.join(d, "img.bin"))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as d:
            fn = os.path.join(d, "p.json")
            with open(fn, "w") as f:
                f.write("{")
            with self.assertRaises(profile.ProfileError):
                profile.load(fn)


class External(unittest.TestCase):
    def test_external_disassembler_parses_and_caches(self):
        import sys
        from ofh.emu import disasm
        script = ("import sys\nprint('00400000 mov 5,d0 ; 01 02 03')\nprint('00400003 add d0,d0 ; 04 05')\nprint('junk line')\n")
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "fake_dis.py")
            with open(sp, "w") as f:
                f.write(script)
            dis = disasm.ExternalDisassembler([sys.executable, sp, "{file}", "{base}", "{start}", "{end}"], cache_dir=os.path.join(d, "c"))
            E.configure(0x400000)
            img = E.Image(bytes(0x20), disasm=dis)
            self.assertEqual(img.dis(0x400000), ("mov", ["5", "d0"], 3))
            self.assertEqual(img.dis(0x400003), ("add", ["d0", "d0"], 2))
            self.assertEqual(dis.calls, 1)
            img2 = E.Image(bytes(0x20), disasm=dis)
            img2.dis(0x400000)
            self.assertEqual(dis.calls, 1, "second image with identical bytes must be served from the window cache")
            img3 = E.Image(bytes([1]) + bytes(0x1f), disasm=dis)     # one changed byte in the hashed window: no stale hit
            img3.dis(0x400000)
            self.assertEqual(dis.calls, 2)
            img.close()

    def test_regex_needs_named_groups(self):
        from ofh.emu import disasm
        with self.assertRaises(ValueError):
            disasm.ExternalDisassembler(["x"], line_regex=r"(\d+)")


if __name__ == "__main__":
    unittest.main()
