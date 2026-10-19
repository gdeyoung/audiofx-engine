#!/usr/bin/env python3
"""Unit tests for the audiofx engine graph composition (pure functions only)."""
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import audiofx  # noqa: E402


def node_names(nodes):
    return [n["name"] for n in nodes]


def find_node(nodes, name):
    for n in nodes:
        if n["name"] == name:
            return n
    return None


class LoudStageTests(unittest.TestCase):
    def test_loud_only_graph(self):
        nodes, links, inputs, outputs, cap = audiofx.build_graph(0, 0, 0, 60)
        self.assertEqual(node_names(nodes), ["convHp_L", "convHp_R", "loudLim"])
        lim = nodes[2]
        self.assertEqual(lim["type"], "ladspa")
        self.assertEqual(lim["plugin"], "fast_lookahead_limiter_1913")
        self.assertEqual(lim["label"], "fastLookaheadLimiter")
        self.assertEqual(lim["control"]["Input gain (dB)"], 3.6)
        self.assertEqual(lim["control"]["Limit (dB)"], -1.0)
        self.assertEqual(lim["control"]["Release time (s)"], 0.02)
        self.assertEqual(inputs, ["convHp_L:In", "convHp_R:In"])
        self.assertEqual(outputs, ["loudLim:Output 1", "loudLim:Output 2"])
        self.assertEqual(cap, ["FL", "FR"])
        pairs = {(l["output"], l["input"]) for l in links}
        self.assertIn(("convHp_L:Out", "loudLim:Input 1"), pairs)
        self.assertIn(("convHp_R:Out", "loudLim:Input 2"), pairs)

    def test_loud_db_mapping(self):
        for loud, db in ((1, 0.06), (50, 3.0), (100, 6.0)):
            _, _, _, _, _ = (None,) * 5
            nodes, _, _, _, _ = audiofx.build_graph(0, 0, 0, loud)
            self.assertEqual(nodes[2]["control"]["Input gain (dB)"], db,
                             "loud=%d" % loud)

    def test_bass_loud_chain_order(self):
        nodes, links, inputs, outputs, _ = audiofx.build_graph(0, 60, 0, 40)
        # bass convs are the first stage (consume capture), hp feeds limiter
        self.assertEqual(inputs, ["convBass_L:In", "convBass_R:In"])
        self.assertEqual(outputs, ["loudLim:Output 1", "loudLim:Output 2"])
        pairs = {(l["output"], l["input"]) for l in links}
        self.assertIn(("convBass_L:Out", "convHp_L:In"), pairs)
        self.assertIn(("convHp_L:Out", "loudLim:Input 1"), pairs)

    def test_spatial_loud_keeps_bank_inputs(self):
        nodes, links, inputs, outputs, cap = audiofx.build_graph(60, 0, 0, 50)
        inputs = inputs or []
        self.assertEqual(len(inputs), 8)
        self.assertTrue(all(i.startswith("copy") for i in inputs))
        self.assertEqual(len(cap), 8)
        self.assertEqual(outputs, ["loudLim:Output 1", "loudLim:Output 2"])
        pairs = {(l["output"], l["input"]) for l in links}
        self.assertIn(("blendL:Out", "convHp_L:In"), pairs)

    def test_loud_zero_regression_no_loud_nodes(self):
        # loud=0 must reproduce the pre-loud graph exactly
        nodes, links, inputs, outputs, _ = audiofx.build_graph(0, 50, 0, 0)
        self.assertNotIn("loudLim", node_names(nodes))
        self.assertNotIn("convHp_L", node_names(nodes))
        self.assertEqual(outputs, ["convBass_L:Out", "convBass_R:Out"])

        nodes, _, _, outputs, _ = audiofx.build_graph(60, 0, 0, 0)
        self.assertNotIn("loudLim", node_names(nodes))
        self.assertEqual(outputs, ["blendL:Out", "blendR:Out"])

    def test_all_zero_still_dies(self):
        with self.assertRaises(SystemExit):
            audiofx.build_graph(0, 0, 0, 0)

    def test_render_fragment_embeds_limiter(self):
        text = audiofx.render_fragment("some.sink", 0, 60, 0, 60)
        self.assertIn("fast_lookahead_limiter_1913", text)
        self.assertIn('"Input gain (dB)" = 3.6', text)
        self.assertIn('"Limit (dB)" = -1.0', text)
        self.assertIn("loudLim:Output 1", text)

    def test_render_fragment_loud_zero_has_no_limiter(self):
        text = audiofx.render_fragment("some.sink", 0, 60, 0, 0)
        self.assertNotIn("fast_lookahead_limiter_1913", text)
        self.assertNotIn("loudLim", text)


class IrgenHpTests(unittest.TestCase):
    def test_hp_ir_shape(self):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "irgen.py"), td, "hp"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            path = os.path.join(td, "hp.wav")
            w = wave.open(path, "rb")
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(),
                              w.getnframes()), (1, 4, 48000, 513))
            raw = w.readframes(513)
            w.close()
            taps = [t / (2 ** 31 - 1) for t in struct.unpack("<513i", raw)]
            dc = sum(taps)  # DC gain = sum of taps
            self.assertAlmostEqual(dc, 10 ** (-30.0 / 20), delta=0.01)
            self.assertAlmostEqual(max(taps), 1.0, delta=0.01)  # HF path unity


if __name__ == "__main__":
    unittest.main()
