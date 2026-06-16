"""
test_determinism.py — guards the prompt-cache prefix invariant.

On API-key Claude Code, the conversation prefix is auto-cached. nexum rewrites
tool output at PostToolUse; that rewrite must be BYTE-DETERMINISTIC, or the
modified output differs between the turn it is produced and later turns,
invalidating the cache prefix from that point on — which costs far more than
any truncation saves. These tests assert that truncate.shrink and the dedup
hook emit identical bytes for identical input across repeated invocations.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import truncate  # noqa: E402
import store      # noqa: E402

# Large enough to trip shrink (> truncate_min_lines_to_act default 240) and to
# include an error line that the keep-regex must retain deterministically.
_BIG_LINES = [f"line {i}: payload content {i * 7}" for i in range(400)]
_BIG_LINES[123] = "ERROR: something failed at step 123"
_BIG_TEXT = "\n".join(_BIG_LINES)


def _run_dedup(payload, data_dir):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "dedup.py")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=15,
    )
    return result.stdout.decode()


class TestShrinkDeterminism(unittest.TestCase):
    def test_shrink_is_byte_stable(self):
        cfg = store.get_config()
        outs = [truncate.shrink(_BIG_TEXT, cfg)[0] for _ in range(5)]
        self.assertTrue(all(o == outs[0] for o in outs))

    def test_shrink_acted(self):
        cfg = store.get_config()
        _, acted = truncate.shrink(_BIG_TEXT, cfg)
        self.assertTrue(acted, "expected shrink to act on a 400-line input")

    def test_shrink_retains_error_line(self):
        cfg = store.get_config()
        out, _ = truncate.shrink(_BIG_TEXT, cfg)
        self.assertIn("ERROR: something failed at step 123", out)


class TestTaskTypeDeterminism(unittest.TestCase):
    """_derive_task_type must not depend on PYTHONHASHSEED.

    A prompt mentioning more than one task type previously resolved via set
    (hash) iteration order, so the intent-guard decision flipped between runs.
    """

    def _task_type(self, prompt, seed):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = str(seed)
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        code = (
            "import context_watch as cw;"
            f"print(cw._signature({prompt!r})[1])"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, env=env, timeout=15,
        )
        return result.stdout.decode().strip()

    def test_mixed_prompt_stable_across_seeds(self):
        prompt = "please debug and integrate the billing module"
        results = {self._task_type(prompt, seed) for seed in range(6)}
        self.assertEqual(len(results), 1,
                         f"task type varied across hash seeds: {results}")


class TestDedupDeterminism(unittest.TestCase):
    def test_fresh_output_emits_identical_bytes(self):
        # Two independent sessions so neither call hits the pointer-collapse
        # path; both take the shrink-and-record branch. The emitted
        # updatedToolOutput must be byte-identical.
        d1 = tempfile.mkdtemp()
        d2 = tempfile.mkdtemp()
        p1 = {"session_id": "a", "tool_name": "Read", "tool_response": _BIG_TEXT}
        p2 = {"session_id": "b", "tool_name": "Read", "tool_response": _BIG_TEXT}
        out1 = _run_dedup(p1, d1)
        out2 = _run_dedup(p2, d2)
        self.assertEqual(out1, out2)


if __name__ == "__main__":
    unittest.main()
