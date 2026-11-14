"""OpenCE 359's join request version must agree with discovery's policy."""
import hashlib
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/xbox'))
import network_bridge as bridge
from test_xbox_network_bridge_294 import browser_sources
from test_xbox_network_bridge_343 import intake
from test_xbox_network_policy import SERVER, engine_tree

LOG = b'\t\t\t\tnetwork_event("join requires %u (client %u)", HALO_PORT_NETWORK_VERSION, version);\n'


def joined_sources():
    sources = browser_sources()
    sources[bridge.CLIENT] += bridge.CLIENT_JOIN
    sources[bridge.LOBBY] = sources[bridge.LOBBY].replace(bridge.LOBBY_NEWER + b'\n', intake())
    sources[bridge.SERVER] = bridge.SERVER_JOIN_DEFINE + SERVER + bridge.SERVER_JOIN + b'\t\t\t{\n' + LOG + b'\t\t\t}\n'
    return sources


class Join359Tests(unittest.TestCase):
    def engine(self, root):
        sources = joined_sources()
        sources[bridge.SERVER_MODE_HEADER] = b'#if defined(HALO_ANDROID)\n#define HALO_SERVER_BUILD 0\n#endif\n'
        for path, (count, _) in bridge.SERVER_ONLY_359.items():
            sources[path] = (b'#if HALO_SERVER_BUILD\n' +
                             b'log_version(HALO_PORT_NETWORK_VERSION);\n' * count + b'#endif\n')
        files = engine_tree(root, sources)
        for patcher in (
            mock.patch.object(bridge, 'SERVER_MODE_SHA256', hashlib.sha256(sources[bridge.SERVER_MODE_HEADER]).hexdigest()),
            mock.patch.object(bridge, 'SERVER_ONLY_359', {
                path: (count, hashlib.sha256(sources[path]).hexdigest())
                for path, (count, _) in bridge.SERVER_ONLY_359.items()}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return files

    def test_request_and_admission_are_bridged_and_sources_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            originals = self.engine(root)
            changes = {p.relative_to(root): changed for p, _, changed in bridge.changes(root)}
            self.assertIn(bridge.CLIENT_JOIN_REPLACE, changes[bridge.CLIENT])
            server = changes[bridge.SERVER]
            self.assertIn(bridge.SERVER_JOIN_REPLACE, server)
            self.assertIn(bridge.SERVER_JOIN_DEFINE + bridge.SERVER_JOIN_DECLARE, server)
            self.assertIn(bridge.SERVER_REPLACE, server)
            self.assertIn(LOG, server)
            self.assertEqual({p: (root / p).read_bytes() for p in originals}, originals)
            self.assertFalse(set(changes) & set(bridge.SERVER_ONLY_359))

    def test_another_use_or_changed_server_source_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            originals = self.engine(root)
            (root / bridge.SERVER).write_bytes(originals[bridge.SERVER] + b'int v = HALO_PORT_NETWORK_VERSION;\n')
            with self.assertRaisesRegex(ValueError, 'network_server_message_handler.c'):
                bridge.changes(root)
            (root / bridge.SERVER).write_bytes(originals[bridge.SERVER])
            for path in bridge.SERVER_ONLY_359:
                with self.subTest(path=path):
                    (root / path).write_bytes(originals[path].replace(b'#if HALO_SERVER_BUILD', b'#if 1'))
                    with self.assertRaisesRegex(ValueError, 'dedicated-server source/guard'):
                        bridge.changes(root)
                    (root / path).write_bytes(originals[path])

    def test_prior_recipe_is_preserved(self):
        self.assertEqual(hashlib.sha256(bridge.recipe(newer=True, browser=True, queued=True)).hexdigest(),
                         'a4173162d563e3e58e59ed76c61eab7353668f1530ec29345c43c610bb1e59b1')
        self.assertNotEqual(bridge.recipe(newer=True, browser=True, queued=True),
                            bridge.recipe(newer=True, browser=True, queued=True, joined=True))
        with self.assertRaises(ValueError):
            bridge.recipe(newer=True, browser=True, joined=True)

    def test_compiled_request_and_admission_follow_the_policy(self):
        # The emitted replacements, compiled against the production host policy.
        source = r'''
#include <assert.h>
#include "xg_network_policy.h"
typedef unsigned short word;
#define HALO_PORT_NETWORK_VERSION 44
POLICY_MODE
static struct xg_network_policy policy;
unsigned host_halopad_network_announce_v1(unsigned ours) { return xg_network_policy_announce(&policy, ours); }
DECLARE
unsigned host_halopad_network_accepts_v1(unsigned ours, unsigned theirs) {
    return xg_network_policy_accepts(&policy, ours, theirs);
}
struct request { word network_version; };
static unsigned requested(void) {
    struct request join_game_request = {0};
REQUEST
    return join_game_request.network_version;
}
static int admitted(unsigned version) {
    struct request join_game_request = {(word)version};
ADMISSION
    { return 0; }
    return 1;
}
int main(void) {
    assert(requested() == 44 && admitted(44) && !admitted(43) && !admitted(45) && !admitted(0));
    assert(xg_network_policy_set(&policy, 44, 45, 44, 45));
#ifdef HALO_ANDROID
    assert(requested() == 45 && admitted(44) && admitted(45) && !admitted(43) && !admitted(46));
#else
    assert(requested() == 44 && admitted(44) && !admitted(45));
#endif
    return 0;
}
'''
        source = (source.replace('DECLARE', bridge.SERVER_JOIN_DECLARE.decode())
                  .replace('REQUEST', bridge.CLIENT_JOIN_REPLACE.decode())
                  .replace('ADMISSION', bridge.SERVER_JOIN_REPLACE.decode()))
        with tempfile.TemporaryDirectory() as tmp:
            for android in (False, True):
                executable = pathlib.Path(tmp) / str(android)
                built = subprocess.run(['clang', '-x', 'c', '-', '-std=gnu89', '-Wall', '-Wextra', '-Werror',
                                        '-Wno-unused-function', '-fsanitize=address,undefined',
                                        '-I', str(ROOT / 'port/xbox'), '-o', str(executable)],
                                       input=source.replace('POLICY_MODE', '#define HALO_ANDROID 1' if android else ''),
                                       text=True, capture_output=True)
                self.assertEqual(built.returncode, 0, built.stderr)
                ran = subprocess.run([str(executable)], capture_output=True)
                self.assertEqual(ran.returncode, 0, ran.stderr.decode())


if __name__ == '__main__':
    unittest.main()
