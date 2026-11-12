"""lab/ui-port.sh: 9090 by default, next free port when another instance holds it, sticky per lab."""
import os
import shutil
import socket
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "lab", "ui-port.sh")


def free_base(span=10):
    """A run of `span` free ports, so the test never depends on what holds 9090 on this machine."""
    for base in range(42000, 60000, 50):
        socks = []
        try:
            for p in range(base, base + span):
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                socks.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise unittest.SkipTest("no free port range")


class TestUiPort(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = free_base()
        self.held = []

    def tearDown(self):
        for s in self.held:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hold(self, port):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        self.held.append(s)

    def resolve(self, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": self.tmp, "CLUSTER": "kc-test-no-such-cluster",
             "WORKDIR": os.path.join(self.tmp, "work"), "KC_DEFAULT_UI_PORT": str(self.base)}
        e.update(env)
        r = subprocess.run(["bash", "-c", f'. "{SCRIPT}" && kc_resolve_ui_port && echo "$UI_PORT|$KUBECOST_URL|$KC_UI_PORT_SOURCE"'],
                           env=e, capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr

    def test_default_then_sticky(self):
        rc, out, _ = self.resolve()
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"{self.base}|http://localhost:{self.base}|default")
        self.assertEqual(open(os.path.join(self.tmp, "work", "ui-port")).read().strip(), str(self.base))
        self.assertTrue(self.resolve()[1].endswith("saved in " + os.path.join(self.tmp, "work", "ui-port")))

    def test_next_free_port_when_default_taken(self):
        self.hold(self.base)
        self.hold(self.base + 1)
        rc, out, _ = self.resolve()
        self.assertEqual(rc, 0)
        port, url, src = out.split("|")
        self.assertEqual(port, str(self.base + 2))
        self.assertIn("in use by another instance", src)

    def test_saved_port_taken_by_someone_else_rescans(self):
        self.resolve()                         # saves base
        self.hold(self.base)                   # another instance grabs it while this lab is down
        self.assertEqual(self.resolve()[1].split("|")[0], str(self.base + 1))

    def test_explicit_port_and_url_win(self):
        self.hold(self.base)
        rc, out, _ = self.resolve(UI_PORT=str(self.base), KUBECOST_URL="http://kubecost.internal:9090")
        self.assertEqual(out.split("|")[:2], [str(self.base), "http://kubecost.internal:9090"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "work", "ui-port")))

    def test_ignores_non_numeric_docker_output(self):
        shim = os.path.join(self.tmp, "bin")
        os.makedirs(shim)
        with open(os.path.join(shim, "docker"), "w") as f:
            f.write("#!/bin/sh\necho 'Docker version 27 (shim)'\n")
        os.chmod(os.path.join(shim, "docker"), 0o755)
        rc, out, _ = self.resolve(PATH=f"{shim}:/usr/bin:/bin")
        self.assertEqual(out.split("|")[0], str(self.base))

    def test_all_taken_and_bad_value(self):
        for p in range(self.base, self.base + 10):
            self.hold(p)
        rc, _, err = self.resolve()
        self.assertNotEqual(rc, 0)
        self.assertIn("all in use", err)
        rc, _, err = self.resolve(UI_PORT="ninety")
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
