"""Unsynchronized frame uploads must not reuse a slot the GPU may still read."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The Khronos headers prepare.sh downloads for the guest and host builds.
KHRONOS = ROOT / 'ref/xbox-build/ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include'

SOURCE = r'''#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include "xg_gl.c"
uintptr_t xg_base;
struct xg_paths xg_paths;
void xg_log(const char *format, ...) { (void)format; }
void xg_fatal(const char *format, ...) { (void)format; abort(); }
GLuint xg_gl_framebuffer(GLuint framebuffer) { return framebuffer; }
static int finished, deleted, fence_fails;
static GLenum wait_result;
static GLsync fence(GLenum condition, GLbitfield flags) { (void)condition; (void)flags; return fence_fails ? NULL : (GLsync)&finished; }
static GLenum client_wait(GLsync sync, GLbitfield flags, GLuint64 timeout) {
    assert(sync == (GLsync)&finished && flags == GL_SYNC_FLUSH_COMMANDS_BIT && timeout == 1000000000ull);
    return wait_result;
}
static void delete_sync(GLsync sync) { assert(sync); deleted++; }
static void finish(void) { finished++; }
static void unused(void) {}
void *xg_gl_proc(const char *name) {
    if (!strcmp(name, "glFenceSync")) return (void *)fence;
    if (!strcmp(name, "glClientWaitSync")) return (void *)client_wait;
    if (!strcmp(name, "glDeleteSync")) return (void *)delete_sync;
    if (!strcmp(name, "glFinish")) return (void *)finish;
    return (void *)unused;
}
int main(void) {
    GLenum completed[] = {GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED};
    for (int i = 0; i < 2; i++) {
        wait_result = completed[i];
        xh_host_gl_fence_frame(1);
        xh_host_gl_wait_frame(1);
        assert(!finished && deleted == i + 1);
    }
    GLenum incomplete[] = {GL_TIMEOUT_EXPIRED, GL_WAIT_FAILED};
    for (int i = 0; i < 2; i++) {
        wait_result = incomplete[i];
        xh_host_gl_fence_frame(2);
        xh_host_gl_wait_frame(2);
        assert(finished == i + 1 && deleted == 3 + i);
    }
    xh_host_gl_wait_frame(2);
    assert(finished == 2);
    fence_fails = 1;
    xh_host_gl_fence_frame(3);
    assert(finished == 3);
    xh_host_gl_wait_frame(3);
    assert(finished == 3);
    return 0;
}
'''


class FrameFenceTests(unittest.TestCase):
    def test_incomplete_or_missing_fence_finishes_queued_work(self):
        # OpenCE 359 fixed this in its Android host (port/android/host/host_gl.c).
        if not (KHRONOS / 'GLES3/gl32.h').is_file():
            self.skipTest('Khronos headers unavailable; run scripts/xbox/prepare.sh')
        with tempfile.TemporaryDirectory() as tmp:
            binary = pathlib.Path(tmp) / 'frame-fences'
            built = subprocess.run(['xcrun', 'clang', '-x', 'c', '-', '-std=gnu11', '-Wall', '-Werror',
                                    '-fsanitize=address,undefined', '-I', str(ROOT / 'port/xbox'),
                                    '-I', str(KHRONOS), '-o', str(binary)],
                                   input=SOURCE, text=True, capture_output=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True)
            self.assertEqual(ran.returncode, 0, ran.stderr.decode())


if __name__ == '__main__':
    unittest.main()
