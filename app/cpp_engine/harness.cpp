// harness.cpp
//
// Linked alongside the user's translation unit. Installs a std::terminate
// handler (before main() runs, via a global object's constructor) so that
// an uncaught C++ exception prints its type and what() message to stderr,
// tagged with markers the backend can grep for, before the process aborts.
//
// Without this, an uncaught exception just looks like "SIGABRT" to the
// outside world - identical to a failed assert() or a corrupted heap. This
// gives users the actual exception type/message instead.

#include <exception>
#include <cstdio>
#include <cstdlib>
#include <typeinfo>
#include <cxxabi.h>

namespace codeviz_harness {

static void print_marker(const char* key, const char* value) {
    std::fprintf(stderr, "\n__CODEVIZ_%s__:%s\n", key, value);
    std::fflush(stderr);
}

static const char* demangle(const char* mangled, char* buf, size_t buf_len) {
    int status = 0;
    size_t len = buf_len;
    char* out = abi::__cxa_demangle(mangled, buf, &len, &status);
    return (status == 0 && out) ? out : mangled;
}

[[noreturn]] static void terminate_handler() {
    std::fprintf(stderr, "\n__CODEVIZ_UNCAUGHT_EXCEPTION__:1\n");
    std::exception_ptr eptr = std::current_exception();
    if (eptr) {
        try {
            std::rethrow_exception(eptr);
        } catch (const std::exception& e) {
            char buf[512];
            print_marker("EXC_TYPE", demangle(typeid(e).name(), buf, sizeof(buf)));
            print_marker("EXC_WHAT", e.what());
        } catch (...) {
            print_marker("EXC_TYPE", "unknown (does not derive from std::exception)");
            print_marker("EXC_WHAT", "(no what() available)");
        }
    } else {
        print_marker("EXC_TYPE", "unknown");
        print_marker("EXC_WHAT", "std::terminate() called with no active exception "
                                  "(likely a noexcept function threw, or terminate() "
                                  "was called directly)");
    }
    std::fflush(stderr);
    std::abort();
}

struct Installer {
    Installer() { std::set_terminate(terminate_handler); }
} installer;

}  // namespace codeviz_harness
