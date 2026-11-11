"""kcctl end to end against the in-process Kubecost stand-in (file state, notifications as dry-run files)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import mock_kubecost  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class TestKcctl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_kubecost.serve()

    def setUp(self):
        mock_kubecost.S.__init__()
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(".git", ".lab", ".kcresize", "__pycache__", "lab.env"))
        port = self.srv.server_port
        self.env = dict(os.environ, NO_COLOR="1", CLUSTER="example-cluster", UI_PORT=str(port), KUBECOST_URL=f"http://127.0.0.1:{port}",
                        OWNER_GITHUB="fraud-owner", OWNER_EMAIL="owner@example.com", ACTIONING_GITHUB="ops-alice",
                        ACTIONING_EMAIL="ops@example.com", KCRESIZE_NOTIFY_DRY_RUN="1", KCRESIZE_NAMESPACES="tbm-payments")
        for k in ("KCCTL_ENV", "KCRESIZE_CONFIG", "KCRESIZE_STATE_FILE", "KCRESIZE_OUTBOX", "GITHUB_REPOSITORY"):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def kc(self, *args, check=True):
        # On macOS run with /bin/bash (3.2) to prove kcctl works with the bash Apple ships
        shell = ["/bin/bash"] if sys.platform == "darwin" and os.path.exists("/bin/bash") else []
        p = subprocess.run([*shell, os.path.join(self.repo, "kcctl"), *args], cwd=self.tmp, env=self.env,
                           capture_output=True, text=True, timeout=120)
        if check and p.returncode != 0:
            self.fail(f"kcctl {' '.join(args)} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
        return p

    def test_flow(self):
        self.assertIn("kcctl 1.", self.kc("version").stdout)
        if sys.platform == "darwin":
            print("\n  /bin/bash:", subprocess.run(["/bin/bash", "--version"], capture_output=True, text=True).stdout.splitlines()[0])
        self.assertIn("Usage: kcctl", self.kc("help").stdout)
        self.kc("init", "--yes")
        envfile = open(os.path.join(self.repo, "lab", "lab.env")).read()
        self.assertIn("CLUSTER=example-cluster", envfile)
        self.assertIn("RUNNER_LABEL=example-cluster", envfile)
        self.assertIn("cluster            example-cluster", self.kc("env").stdout)
        subprocess.run([os.path.join(self.repo, "lab", "setup.sh"), "config"], env=self.env, check=True, capture_output=True)
        self.assertIn("doctor: OK", self.kc("doctor").stdout)
        self.assertIn("Read-only mode", self.kc("plan", "--read-only").stdout)
        self.kc("plan")
        self.assertIn("pending", self.kc("status").stdout)
        self.assertEqual(self.kc("disapprove", "tbm-payments/fraud-scorer", check=False).returncode, 3)   # reason required
        self.kc("approve", "tbm-payments/fraud-scorer", "--reason", "traffic is flat")
        self.assertEqual(self.kc("apply", "tbm-payments/fraud-scorer", check=False).returncode, 3)        # owner is not actioning
        self.kc("apply", "tbm-payments/fraud-scorer", "--as", "ops-alice")
        self.assertIn("applied: tbm-payments/fraud-scorer", self.kc("watch").stdout)
        self.assertRegex(self.kc("status").stdout, r"tbm-payments/fraud-scorer\s+applied")
        outbox = os.listdir(os.path.join(self.repo, ".lab", "example-cluster", "outbox"))
        self.assertTrue(any(f.endswith("email.json") for f in outbox))

    def test_misc(self):
        self.assertIn("complete -F _kcctl kcctl", self.kc("completion", "bash").stdout)
        self.assertEqual(self.kc("bogus", check=False).returncode, 1)
        bindir = os.path.join(self.tmp, "bin")
        self.kc("install", bindir)
        link = os.path.join(bindir, "kcctl")
        self.assertTrue(os.path.islink(link))
        p = subprocess.run([link, "version"], env=self.env, capture_output=True, text=True)   # works through the symlink
        self.assertIn("kcctl 1.", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
