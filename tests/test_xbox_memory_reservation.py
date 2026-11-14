"""Exercise the production guest allocator, including its address contract."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class MemoryReservationTests(unittest.TestCase):
    def test_real_aligned_reservation_mapping_and_unmapping(self):
        source = r'''
#include "xg_host.h"
#include <assert.h>
#include <mach/mach.h>
#include <stdlib.h>
#include <sys/mman.h>
char xg_text[1];
void xg_log(const char *format, ...) { (void)format; }
void xg_fatal(const char *format, ...) { (void)format; abort(); }
int xg_linux_errno(int error) { return error; }
int main(void) {
    assert(xg_memory_initialize() == 0);
    assert(xg_base && !(xg_base & 0xffffffffULL));
    volatile unsigned char *window = G(volatile unsigned char *, XG_WINDOW_BASE);
    window[0] = 17; window[XG_WINDOW_SIZE - 1] = 29;
    assert(window[0] == 17 && window[XG_WINDOW_SIZE - 1] == 29);
    uint32_t guest = xg_map(XG_PAGE, PROT_READ | PROT_WRITE);
    assert(guest >= 0x10000000 && guest < XG_WINDOW_BASE);
    unsigned char *host = G(unsigned char *, guest);
    assert(GA(host) == guest && !host[0] && !host[XG_PAGE - 1]);
    host[0] = 43; host[XG_PAGE - 1] = 71;
    assert(host[0] == 43 && host[XG_PAGE - 1] == 71);
    xg_unmap(guest, XG_PAGE);
    unsigned char byte = 0; vm_size_t read = 0;
    assert(vm_read_overwrite(mach_task_self(), (vm_address_t)host, 1,
        (vm_address_t)&byte, &read) != KERN_SUCCESS);
    assert(xg_guest_mmap(0xffe00000, XG_PAGE, PROT_READ | PROT_WRITE,
        0x10 | 0x20, -1, 0) == 0xffe00000L);
    host = G(unsigned char *, 0xffe00000);
    host[0] = 113; assert(host[0] == 113);
    assert(vm_deallocate(mach_task_self(), xg_base, 0x100000000ULL) == KERN_SUCCESS);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as folder:
            binary = pathlib.Path(folder) / 'memory-test'
            subprocess.run(['xcrun', 'clang', '-O2', '-Wall', '-Werror',
                            '-I', str(ROOT / 'port/xbox'), '-x', 'c', '-',
                            str(ROOT / 'port/xbox/xg_memory.c'), '-o', str(binary)],
                           input=source, text=True, capture_output=True, check=True)
            subprocess.run([str(binary)], capture_output=True, check=True, timeout=15)

    def test_mcc_tag_window_is_kept_out_of_allocations_and_reserved_on_request(self):
        # OpenCE 359's host_mcc_memory_* contract (port/android/host/host_mcc_memory.c).
        source = r'''
#include "xg_host.h"
#include <assert.h>
#include <errno.h>
#include <mach/mach.h>
#include <stdlib.h>
#include <sys/mman.h>
char xg_text[1];
void xg_log(const char *format, ...) { (void)format; }
void xg_fatal(const char *format, ...) { (void)format; abort(); }
int xg_linux_errno(int error) { return error; }
int64_t xh_host_mcc_memory_reserve(uint32_t address, uint32_t size);
int xh_host_mcc_memory_release(uint32_t address, uint32_t size);
static int readable(uint32_t guest) {
    unsigned char byte = 0; vm_size_t read = 0;
    return vm_read_overwrite(mach_task_self(), (vm_address_t)G(void *, guest), 1,
        (vm_address_t)&byte, &read) == KERN_SUCCESS;
}
int main(void) {
    const uint32_t base = 0x50000000u, size = 0x04000000u;
    assert(xg_memory_initialize() == 0);
    /* everything below the default window, then the allocator steps over it */
    assert(xg_map(base - 0x10000000u, PROT_READ | PROT_WRITE) == 0x10000000u);
    uint32_t after = xg_map(0x08000000u, PROT_READ | PROT_WRITE);
    assert(after >= base + size);
    assert(!readable(base) && !readable(base + size - 1));
    assert(xh_host_mcc_memory_reserve(base + 0x1000u, size) == -EINVAL);
    assert(xh_host_mcc_memory_reserve(base, size / 2) == -EINVAL);
    assert(xh_host_mcc_memory_reserve(0x40000000u, size) == -EINVAL);
    assert(xh_host_mcc_memory_release(base, size) == -EINVAL);
    assert(xh_host_mcc_memory_reserve(base, size) == base);
    unsigned char *tags = G(unsigned char *, base);
    assert(!tags[0] && !tags[size - 1]);
    tags[0] = 31; tags[size - 1] = 37;
    assert(xh_host_mcc_memory_reserve(base, size) == -EBUSY);
    assert(xh_host_mcc_memory_release(base, size) == 0);
    assert(!readable(base));
    /* a window over ordinary allocations is refused, the old one given up */
    assert(xh_host_mcc_memory_reserve(0x48000000u, size) == -EEXIST);
    assert(xh_host_mcc_memory_reserve(after, size) == -EEXIST);
    uint32_t moved = 0x60000000u;
    assert(moved >= after + 0x08000000u);
    assert(xh_host_mcc_memory_reserve(moved, size) == moved);
    G(unsigned char *, moved)[0] = 41;
    assert(xh_host_mcc_memory_release(moved, size) == 0);
    /* once ordinary memory there is freed, the window may move into it */
    xg_unmap(0x10000000u, base - 0x10000000u);
    assert(xh_host_mcc_memory_reserve(0x48000000u, size) == 0x48000000u);
    G(unsigned char *, 0x48000000u)[size - 1] = 43;
    assert(xh_host_mcc_memory_release(0x48000000u, size) == 0 && !readable(0x48000000u));
    assert(vm_deallocate(mach_task_self(), xg_base, 0x100000000ULL) == KERN_SUCCESS);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as folder:
            binary = pathlib.Path(folder) / 'mcc-memory-test'
            subprocess.run(['xcrun', 'clang', '-O2', '-Wall', '-Werror',
                            '-I', str(ROOT / 'port/xbox'), '-x', 'c', '-',
                            str(ROOT / 'port/xbox/xg_memory.c'), '-o', str(binary)],
                           input=source, text=True, capture_output=True, check=True)
            subprocess.run([str(binary)], capture_output=True, check=True, timeout=15)
