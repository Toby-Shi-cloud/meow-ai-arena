// arena.cpp —— 2v2 足球 裁判 / 对局程序（Unix：macOS + Linux）
//
// 编译：clang++ -std=c++17 -O2 -pthread arena.cpp -o arena
//       （Linux 也可以用 g++；不需要 -static，也不需要额外链接库）
//
// 用法（规则全部来自 soccer.hpp）：
//   arena play <A1> <A2> <B1> <B2> [选项]
//       打一局。选项：--seed S（默认 1）--first-kick 0|1（默认 0）--names a,b,c,d --replay 文件 --json 文件
//       不给 --json 时结果 JSON 打到标准输出；同时在标准错误打一行可读的摘要。
//   arena batch [选项] < 任务
//       任务（UTF-8）：若干行 "BOT <编号> <名字> <路径>"（名字不能有空格，路径可以有），
//                     或 "BOTCMD <编号> <名字> <整条命令行>"（测试用，例如 python3 proto_bot.py pro_b0）
//                     若干行 "GAME <局号> <种子> <开球队> <A1 编号> <A2 编号> <B1 编号> <B2 编号>"
//       每局结束在标准输出打一行 JSON（按完成顺序）。致命错误打一行 "X 说明"，退出码 3。
//       选项：--workers K（并行局数，默认 CPU 逻辑核数的一半，至少 1）--replay-dir 目录（回放写成 g<局号>.txt）
//   arena calib            兼容保留；Unix 版直接按 CPU 毫秒记账，打印 1
//   共同选项：
//     --step-ms 10         每步 CPU 预算（毫秒）
//     --first-ms 1000      第 0 步的 CPU 预算（从进程启动算起，含读开局信息）
//     --wall-ms 1000       每步墙上时钟上限：回复在截止时刻之后才到 → 判"失去响应"，结束进程树，之后每步按 0 0 0 0 0
//     --first-wall-ms 5000 第 0 步的墙上时钟上限
//     --max-timeouts 30    累计超时达到这个次数 → 结束进程树，之后每步按 0 0 0 0 0（0 = 不限）
//     --stderr-dir 目录    选手的标准错误写到 目录/g<局号>_p<球员号>.txt（每人每局最多 1 MB，默认丢弃）
//     --cfg KEY=VAL        改规则参数（只用于实验；正式比赛不用），可重复
//     --mem-mb、--max-procs、--sandbox
//                          为兼容旧调用仍接受，但 Unix 版不内置沙箱，会忽略并提示
//
// 计时：每名选手是裁判直接启动的一个子进程。每步的 CPU 用量 = 本步回复读到时该进程的累计 CPU 时间
//       − 上一步回复读到时的读数（第 0 步从 0 算起）。进程退出后用 wait4 的 rusage 校正最后一次读数。
//       Linux 用 clock_getcpuclockid（退回 /proc/<pid>/stat），macOS 用 proc_pid_rusage；都是毫秒以下的精度。
// 超时（CPU 超预算）：本步动作作废（按 0 0 0 0 0），照常等它的回复以保持同步。
// 非法输出（不是恰好 5 个整数、数超出 ±10^9、行太长）：本步按 0 0 0 0 0。空行忽略。
// 崩溃 / 退出 / 关闭标准输出 / 失去响应 / 输出超量：结束整个进程组，本步和之后每步都按 0 0 0 0 0。
//
// 与 Windows 版的区别：AppContainer / Job Object 沙箱已删除。不能开子进程、不能读写文件、
// 不能联网等限制由外部运行环境提供；本地直接运行选手程序。JSON 里的 "sandbox" 恒为 false。

#if defined(__linux__) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE  // wait4、clock_getcpuclockid、strtok_r
#endif

#include "soccer.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <memory>
#include <mutex>
#include <poll.h>
#include <string>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <vector>

#ifdef __APPLE__
#include <libproc.h>
#include <mach/mach_time.h>
#endif

using soccer::i64;

extern char** environ;  // execve 用；fork 之后仍然可用

namespace {

// -------------------------------------------------------------------- 设置与上限

struct Limits {
    double step_ms = 10, first_ms = 1000, wall_ms = 1000, first_wall_ms = 5000;
    int max_timeouts = 30;
};

constexpr size_t MAX_LINE = 1000;             // 一行超过这么多字符 → 不合法
constexpr size_t MAX_PARTIAL = 100000;        // 没有换行的数据攒到这么多 → 丢掉，记一次"一行太长"
constexpr size_t MAX_PENDING = 64;            // 还没被裁判取走的完整行超过这么多 → 输出超量，结束进程组
constexpr size_t MAX_OUT_BYTES = 8u << 20;    // 一局里标准输出总字节数上限（正常一局约 20 KB）
constexpr size_t STDERR_CAP = 1u << 20;       // 每人每局保存的标准错误上限

Limits g_lim;
soccer::Config g_cfg;
std::string g_stderr_dir, g_replay_dir;
std::mutex g_out_mu;
std::atomic<unsigned long> g_tmp_counter{0};

void emit(const std::string& line) {
    std::lock_guard<std::mutex> lk(g_out_mu);
    if (std::fputs(line.c_str(), stdout) < 0 || std::fputc('\n', stdout) == EOF || std::fflush(stdout) != 0)
        _Exit(4);  // 结果送不出去：不能假装成功
}

[[noreturn]] void fatal(const std::string& what) {
    {
        std::lock_guard<std::mutex> lk(g_out_mu);
        std::fputs(("X " + what + "\n").c_str(), stdout);
        std::fflush(stdout);
    }
    std::fflush(nullptr);
    _Exit(3);
}

std::string err_str(int e) { return std::strerror(e); }

// 把任意字节串修成合法 UTF-8：不合法的字节换成 U+FFFD
std::string utf8_fix(const std::string& s) {
    std::string o;
    o.reserve(s.size());
    size_t i = 0;
    const size_t n = s.size();
    while (i < n) {
        const unsigned char c = (unsigned char)s[i];
        size_t len = 0;
        unsigned cp = 0;
        if (c < 0x80) len = 1, cp = c;
        else if ((c & 0xE0) == 0xC0) len = 2, cp = c & 0x1F;
        else if ((c & 0xF0) == 0xE0) len = 3, cp = c & 0x0F;
        else if ((c & 0xF8) == 0xF0) len = 4, cp = c & 0x07;
        bool ok = len > 0 && i + len <= n;
        for (size_t k = 1; ok && k < len; ++k) {
            const unsigned char d = (unsigned char)s[i + k];
            if ((d & 0xC0) != 0x80) ok = false;
            else cp = (cp << 6) | (d & 0x3F);
        }
        if (ok && ((len == 2 && cp < 0x80) || (len == 3 && cp < 0x800) || (len == 4 && (cp < 0x10000 || cp > 0x10FFFF)) ||
                   (cp >= 0xD800 && cp <= 0xDFFF)))
            ok = false;
        if (ok) {
            o.append(s, i, len);
            i += len;
        } else {
            o += "\xEF\xBF\xBD";
            ++i;
        }
    }
    return o;
}

// 选手原始输出 → 纯 ASCII：可打印字符原样（反斜杠写成 \\），其余字节写成 \xHH；最多 maxlen 个原始字节
std::string safe_raw(const std::string& s, size_t maxlen = 80) {
    std::string o;
    const size_t n = s.size() < maxlen ? s.size() : maxlen;
    for (size_t i = 0; i < n; ++i) {
        const unsigned char c = (unsigned char)s[i];
        if (c == '\\') o += "\\\\";
        else if (c >= 0x20 && c < 0x7F) o += char(c);
        else {
            char b[8];
            std::snprintf(b, sizeof b, "\\x%02X", c);
            o += b;
        }
    }
    if (s.size() > maxlen) o += "...";
    return o;
}

std::string json_str(const std::string& raw) {
    const std::string s = utf8_fix(raw);
    std::string o = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') o += '\\', o += char(c);
        else if (c < 0x20 || c == 0x7F) {
            char b[8];
            std::snprintf(b, sizeof b, "\\u%04x", c);
            o += b;
        } else o += char(c);
    }
    return o + "\"";
}

std::string num2(double v) {
    char b[32];
    std::snprintf(b, sizeof b, "%.3f", v);
    return b;
}

// 单调时钟，毫秒
double now_ms() {
    using clock = std::chrono::steady_clock;
    static const clock::time_point t0 = clock::now();
    return std::chrono::duration<double, std::milli>(clock::now() - t0).count();
}

std::string normalize_path(const std::string& p) {
    const bool abs = !p.empty() && p[0] == '/';
    std::vector<std::string> parts;
    size_t i = 0;
    while (i <= p.size()) {
        size_t j = p.find('/', i);
        if (j == std::string::npos) j = p.size();
        const std::string part = p.substr(i, j - i);
        if (part == "..") {
            if (!parts.empty()) parts.pop_back();
        } else if (!part.empty() && part != ".") {
            parts.push_back(part);
        }
        if (j == p.size()) break;
        i = j + 1;
    }
    std::string out = abs ? "/" : "";
    for (size_t k = 0; k < parts.size(); ++k) {
        if (k) out += '/';
        out += parts[k];
    }
    return out.empty() ? (abs ? "/" : ".") : out;
}

std::string absolute_path(const std::string& p) {
    if (p.empty()) return p;
    if (p[0] == '/') return normalize_path(p);
    char buf[4096];
    if (!getcwd(buf, sizeof buf)) return p;
    return normalize_path(std::string(buf) + "/" + p);
}

std::string dirname_of(const std::string& p) {
    const size_t cut = p.find_last_of('/');
    if (cut == std::string::npos) return ".";
    if (cut == 0) return "/";
    return p.substr(0, cut);
}

std::string join_path(const std::string& dir, const std::string& name) {
    if (dir.empty()) return name;
    if (dir.back() == '/') return dir + name;
    return dir + "/" + name;
}

bool ensure_dir(const std::string& path) {
    if (path.empty()) return true;
    std::string cur;
    size_t i = 0;
    if (path[0] == '/') {
        cur = "/";
        i = 1;
    }
    while (i <= path.size()) {
        size_t j = path.find('/', i);
        if (j == std::string::npos) j = path.size();
        const std::string part = path.substr(i, j - i);
        if (!part.empty() && part != ".") {
            if (!cur.empty() && cur.back() != '/') cur += '/';
            cur += part;
            if (mkdir(cur.c_str(), 0777) != 0 && errno != EEXIST) return false;
        }
        if (j == path.size()) break;
        i = j + 1;
    }
    return true;
}

// 写文件：先写临时文件、检查每次写入的字节数、落盘、关闭，全部成功后再原子替换目标。失败返回错误说明（空 = 成功）
std::string write_file_atomic(const std::string& path, const std::string& data) {
    const std::string tmp = path + ".tmp" + std::to_string(getpid()) + "_" +
                            std::to_string(g_tmp_counter.fetch_add(1));
    const int fd = open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0) return "建不了文件（" + err_str(errno) + "）";
    size_t done = 0;
    std::string err;
    while (done < data.size()) {
        const ssize_t w = write(fd, data.data() + done, data.size() - done);
        if (w < 0) {
            if (errno == EINTR) continue;
            err = "写入失败（" + err_str(errno) + "，已写 " + std::to_string(done) + " / " +
                  std::to_string(data.size()) + " 字节）";
            break;
        }
        if (w == 0) {
            err = "写入失败（没有进展）";
            break;
        }
        done += size_t(w);
    }
    if (err.empty() && fsync(fd) != 0) err = "落盘失败（" + err_str(errno) + "）";
    if (close(fd) != 0 && err.empty()) err = "关闭失败（" + err_str(errno) + "）";
    if (err.empty() && rename(tmp.c_str(), path.c_str()) != 0)
        err = "替换目标文件失败（" + err_str(errno) + "）";
    if (!err.empty()) unlink(tmp.c_str());
    return err;
}

// -------------------------------------------------------------------- 进程 CPU 计量

// 直接子进程当前的累计 CPU 时间（用户 + 内核，毫秒）。失败返回 -1。
double live_cpu_ms(pid_t pid) {
#if defined(__APPLE__)
    // proc_pid_rusage 的时间是 mach absolute time 的滴答数；Apple Silicon 上是 24 MHz，
    // Intel 上是 1 GHz，不能直接当纳秒用。
    static mach_timebase_info_data_t tb = [] {
        mach_timebase_info_data_t t {};
        mach_timebase_info(&t);
        return t;
    }();
    struct rusage_info_v2 ri {};
    if (proc_pid_rusage(pid, RUSAGE_INFO_V2, (rusage_info_t*)&ri) == 0) {
        const double ns = double(ri.ri_user_time + ri.ri_system_time) * double(tb.numer) / double(tb.denom);
        return ns / 1e6;
    }
    return -1;
#elif defined(__linux__)
    clockid_t clk;
    if (clock_getcpuclockid(pid, &clk) == 0) {
        struct timespec ts {};
        if (clock_gettime(clk, &ts) == 0)
            return double(ts.tv_sec) * 1000.0 + double(ts.tv_nsec) / 1e6;
    }
    // 退回 /proc/<pid>/stat（utime + stime，单位是时钟滴答）
    char p[64];
    std::snprintf(p, sizeof p, "/proc/%d/stat", int(pid));
    FILE* f = std::fopen(p, "r");
    if (!f) return -1;
    char buf[4096];
    const size_t n = std::fread(buf, 1, sizeof buf - 1, f);
    std::fclose(f);
    buf[n] = '\0';
    char* rp = std::strrchr(buf, ')');
    if (!rp) return -1;
    unsigned long long utime = 0, stime = 0;
    int field = 3;  // ")" 之后第一个空格分隔字段是第 3 项（state）
    char* save = nullptr;
    for (char* tok = strtok_r(rp + 1, " \t\n", &save); tok; tok = strtok_r(nullptr, " \t\n", &save), ++field) {
        if (field == 14) utime = std::strtoull(tok, nullptr, 10);
        else if (field == 15) {
            stime = std::strtoull(tok, nullptr, 10);
            break;
        }
    }
    long hz = sysconf(_SC_CLK_TCK);
    if (hz <= 0) hz = 100;
    return double(utime + stime) * 1000.0 / double(hz);
#else
    (void)pid;
    return -1;
#endif
}

struct CpuTracker {
    std::mutex mu;
    pid_t pid = -1;
    double last = 0;
    double final_ms = 0;
    bool has_final = false;

    double ms() {
        std::lock_guard<std::mutex> lk(mu);
        double v = has_final ? final_ms : (pid > 0 ? live_cpu_ms(pid) : -1);
        if (v < 0) v = last;
        if (v < last) v = last;  // 读数不倒退
        last = v;
        return v;
    }

    void set_final(double v) {
        std::lock_guard<std::mutex> lk(mu);
        if (v > last) last = v;
        final_ms = last;
        has_final = true;
    }
};

// -------------------------------------------------------------------- 读线程（标准输出 / 标准错误）

struct Notifier {
    std::mutex mu;
    std::condition_variable cv;
    void notify() {
        std::lock_guard<std::mutex> lk(mu);
        cv.notify_all();
    }
};

struct Line {
    std::string text;  // 太长的行为空
    double cpu_ms;     // 读到这一行时进程的累计 CPU
    double recv_ms;    // 读到这一行时的墙上时钟
    bool too_long;
    size_t len;
};

// 读线程和 Player 共享的状态
struct OutState {
    std::mutex mu;
    std::deque<Line> lines;
    bool eof = false;
    bool flood = false;
    std::string flood_why;
    size_t total = 0;
    std::atomic<bool> stop{false};
    int fd = -1;       // 读端；读线程负责关闭
    pid_t pid = -1;    // 进程组号；输出超量时读线程直接结束整个进程组
    std::shared_ptr<Notifier> note;
    std::shared_ptr<CpuTracker> cpu;
};

void out_reader(std::shared_ptr<OutState> st) {
    std::string buf;
    char chunk[4096];
    bool overflow = false;
    auto flood = [&](const std::string& why) {
        {
            std::lock_guard<std::mutex> lk(st->mu);
            st->flood = true;
            st->flood_why = why;
        }
        if (st->pid > 0) {
            kill(-st->pid, SIGKILL);
            kill(st->pid, SIGKILL);
        }
    };
    while (!st->stop) {
        struct pollfd p {};
        p.fd = st->fd;
        p.events = POLLIN;
        const int r = poll(&p, 1, 100);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (r == 0) continue;
        const ssize_t n = read(st->fd, chunk, sizeof chunk);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            break;
        }
        if (n == 0) break;
        st->total += size_t(n);
        if (st->total > MAX_OUT_BYTES) {
            flood("标准输出累计超过 " + std::to_string(MAX_OUT_BYTES >> 20) + " MB");
            break;
        }
        buf.append(chunk, size_t(n));
        size_t pos;
        bool stop_now = false;
        while ((pos = buf.find('\n')) != std::string::npos) {
            std::string line = buf.substr(0, pos);
            buf.erase(0, pos + 1);
            const bool too_long = overflow || line.size() > MAX_LINE;
            const size_t len = overflow ? MAX_PARTIAL + line.size() : line.size();
            overflow = false;
            if (!too_long && line.find_first_not_of(" \t\r") == std::string::npos) continue;  // 空行：忽略（但计入总字节）
            const double cpu_ms = st->cpu->ms();
            const double rt = now_ms();
            size_t pending;
            {
                std::lock_guard<std::mutex> lk(st->mu);
                st->lines.push_back({too_long ? std::string() : line, cpu_ms, rt, too_long, len});
                pending = st->lines.size();
            }
            st->note->notify();
            if (pending > MAX_PENDING) {
                flood("未被取走的输出行超过 " + std::to_string(MAX_PENDING) + " 行（每步只应输出一行）");
                stop_now = true;
                break;
            }
        }
        if (stop_now) break;
        if (buf.size() > MAX_PARTIAL) {  // 一行太长：丢掉，记一次非法
            buf.clear();
            overflow = true;
        }
    }
    {
        std::lock_guard<std::mutex> lk(st->mu);
        st->eof = true;
    }
    st->note->notify();
    if (st->fd >= 0) {
        close(st->fd);
        st->fd = -1;
    }
}

struct ErrState {
    std::atomic<bool> stop{false};
    int fd = -1;   // 读端；读线程负责关闭
    int out = -1;  // 保存文件；读线程负责关闭
};

// 标准错误：一直读（不让选手卡在写 stderr 上），只保存前 STDERR_CAP 字节
void err_reader(std::shared_ptr<ErrState> st) {
    char chunk[4096];
    size_t saved = 0;
    bool noted = false;
    while (!st->stop) {
        struct pollfd p {};
        p.fd = st->fd;
        p.events = POLLIN;
        const int r = poll(&p, 1, 100);
        if (r < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (r == 0) continue;
        const ssize_t n = read(st->fd, chunk, sizeof chunk);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            break;
        }
        if (n == 0) break;
        if (saved < STDERR_CAP) {
            const size_t k = std::min<size_t>(size_t(n), STDERR_CAP - saved);
            size_t done = 0;
            while (done < k) {
                const ssize_t w = write(st->out, chunk + done, k - done);
                if (w < 0) {
                    if (errno == EINTR) continue;
                    break;
                }
                done += size_t(w);
            }
            saved += done;
        }
        if (saved >= STDERR_CAP && !noted) {
            static const char msg[] = "\n[arena：标准错误超过 1 MB，之后的内容丢弃]\n";
            size_t done = 0;
            while (done < sizeof msg - 1) {
                const ssize_t w = write(st->out, msg + done, sizeof msg - 1 - done);
                if (w < 0) {
                    if (errno == EINTR) continue;
                    break;
                }
                done += size_t(w);
            }
            noted = true;
        }
    }
    if (st->fd >= 0) close(st->fd);
    if (st->out >= 0) close(st->out);
}

void set_cloexec(int fd) {
    const int fl = fcntl(fd, F_GETFD);
    if (fl >= 0) fcntl(fd, F_SETFD, fl | FD_CLOEXEC);
}

// 把一整条命令行拆成 argv（支持单引号、双引号、反斜杠转义；不做变量展开和通配）
std::vector<std::string> split_command(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    bool any = false, esc = false;
    char quote = 0;
    for (const char c : s) {
        if (esc) {
            cur += c;
            esc = false;
            any = true;
        } else if (c == '\\' && quote != '\'') {
            esc = true;
            any = true;
        } else if (quote) {
            if (c == quote) quote = 0;
            else cur += c;
            any = true;
        } else if (c == '\'' || c == '"') {
            quote = c;
            any = true;
        } else if (c == ' ' || c == '\t') {
            if (any) {
                out.push_back(cur);
                cur.clear();
                any = false;
            }
        } else {
            cur += c;
            any = true;
        }
    }
    if (any) out.push_back(cur);
    return out;
}

// BOTCMD 的第一个词：有 / 就按路径处理，否则在 PATH 里找
std::string resolve_cmd(const std::string& cmd) {
    if (cmd.find('/') != std::string::npos) return absolute_path(cmd);
    const char* path = std::getenv("PATH");
    if (path) {
        std::string rest = path;
        while (!rest.empty()) {
            const size_t cut = rest.find(':');
            const std::string dir = rest.substr(0, cut);
            const std::string cand = join_path(dir.empty() ? "." : dir, cmd);
            if (access(cand.c_str(), X_OK) == 0) return cand;
            if (cut == std::string::npos) break;
            rest.erase(0, cut + 1);
        }
    }
    return cmd;
}

// -------------------------------------------------------------------- 一名选手（一个进程组）

enum Status { ST_OK, ST_START_FAIL, ST_CRASH, ST_LOST, ST_TIMEOUT_LIMIT, ST_OUTPUT_LIMIT };
const char* STATUS_NAME[] = {"ok", "start_fail", "crash", "lost", "timeout_limit", "output_limit"};

struct Fault {
    i64 t;
    std::string kind, detail;
};

struct Player {
    std::string path;
    std::string cmdline;  // 非空时直接用这条命令行启动（BOTCMD，只用于测试）
    int slot = 0;         // 球员号 0..3
    int game = 0;
    pid_t pid = -1;       // 子进程；同时是进程组号
    int in_w = -1;        // 选手标准输入（非阻塞）
    std::thread rd_thread, er_thread;
    bool rd_started = false, er_started = false;
    std::shared_ptr<OutState> out;
    std::shared_ptr<ErrState> err;
    std::shared_ptr<CpuTracker> cpu;
    std::shared_ptr<Notifier> note;
    bool dead = false;  // 不再询问（崩溃 / 失联 / 超时过多 / 启动失败 / 输出超量）
    bool finished = false;
    bool reaped = false;
    Status status = ST_OK;
    i64 dead_step = -1;
    double mark = 0;
    double asked_at = 0;
    bool waiting = false;
    // 统计
    double first_ms = 0, sum_ms = 0, max_ms = 0, total_cpu_ms = 0, job_cpu_ms = -1;
    int counted = 0, timeouts = 0, invalid = 0;
    int exit_code = 0, term_sig = 0;
    std::vector<Fault> faults;

    ~Player() { finish(); }

    void start_failed(const std::string& why) {
        dead = true;
        status = ST_START_FAIL;
        dead_step = 0;
        faults.push_back({0, "start_fail", why});
    }

    std::string exit_desc() const {
        char b[160];
        if (term_sig) std::snprintf(b, sizeof b, "被信号 %d 结束", term_sig);
        else std::snprintf(b, sizeof b, "退出码 %d", exit_code);
        return b;
    }

    bool reap_nonblock() {
        if (reaped) return true;
        if (pid <= 0) return false;
        int status = 0;
        struct rusage ru {};
        pid_t r;
        do {
            r = wait4(pid, &status, WNOHANG, &ru);
        } while (r < 0 && errno == EINTR);
        if (r == 0) return false;
        if (r < 0) {  // ECHILD：已经被别人回收
            reaped = true;
            return true;
        }
        reaped = true;
        if (WIFEXITED(status)) exit_code = WEXITSTATUS(status);
        else if (WIFSIGNALED(status)) {
            term_sig = WTERMSIG(status);
            exit_code = 128 + term_sig;
        }
        if (cpu) {
            const double ms = double(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) * 1000.0 +
                              double(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) / 1000.0;
            cpu->set_final(ms);
        }
        return true;
    }

    bool start(const std::string& header, const std::shared_ptr<Notifier>& nt) {
        note = nt;
        cpu = std::make_shared<CpuTracker>();
        const bool test_cmd = !cmdline.empty();

        std::vector<std::string> av;
        if (test_cmd) {
            av = split_command(cmdline);
            if (av.empty()) {
                start_failed("BOTCMD 是空的");
                return false;
            }
            av[0] = resolve_cmd(av[0]);
        } else {
            av.push_back(absolute_path(path));
        }
        const std::string cwd = test_cmd ? "." : dirname_of(av[0]);

        int in_p[2] = {-1, -1}, out_p[2] = {-1, -1}, err_p[2] = {-1, -1}, exec_p[2] = {-1, -1};
        const bool need_err = !g_stderr_dir.empty();
        int err_file = -1;
        auto close_pipes = [&] {
            for (int* p : {in_p, out_p, err_p, exec_p})
                for (int k = 0; k < 2; ++k)
                    if (p[k] >= 0) close(p[k]);
            if (err_file >= 0) close(err_file);
        };
        if (pipe(in_p) != 0 || pipe(out_p) != 0 || pipe(exec_p) != 0) {
            close_pipes();
            fatal("建管道失败（" + err_str(errno) + "）");
        }
        if (need_err) {
            if (pipe(err_p) != 0) {
                close_pipes();
                fatal("建管道失败（" + err_str(errno) + "）");
            }
            err_file = open(join_path(g_stderr_dir, "g" + std::to_string(game) + "_p" +
                                                          std::to_string(slot) + ".txt")
                                .c_str(),
                            O_WRONLY | O_CREAT | O_TRUNC, 0666);
            if (err_file < 0) {
                close_pipes();
                fatal("写不了标准错误文件：" + g_stderr_dir + "（" + err_str(errno) + "）");
            }
        }
        // 父进程保留的 fd 不能漏给选手；exec 错误管道写端在 exec 成功后自动关闭
        set_cloexec(in_p[1]);
        set_cloexec(out_p[0]);
        set_cloexec(exec_p[0]);
        set_cloexec(exec_p[1]);
        if (need_err) set_cloexec(err_p[0]);
        if (err_file >= 0) set_cloexec(err_file);

        std::vector<char*> argv;
        argv.reserve(av.size() + 1);
        for (auto& s : av) argv.push_back(const_cast<char*>(s.c_str()));
        argv.push_back(nullptr);
        char* const* argv_c = argv.data();
        const char* cwd_c = cwd.c_str();

        const pid_t child = fork();
        if (child < 0) {
            close_pipes();
            fatal("fork 失败（" + err_str(errno) + "）");
        }
        if (child == 0) {
            // 子进程：只调用 async-signal-safe 的函数，直到 exec
            setpgid(0, 0);
            auto child_fail = [&] {
                const int e = errno;
                (void)!write(exec_p[1], &e, sizeof e);
                _exit(127);
            };
            if (chdir(cwd_c) != 0) child_fail();
            if (dup2(in_p[0], 0) < 0 || dup2(out_p[1], 1) < 0) child_fail();
            if (need_err) {
                if (dup2(err_p[1], 2) < 0) child_fail();
            } else {
                const int nul = open("/dev/null", O_WRONLY);
                if (nul >= 0) {
                    if (dup2(nul, 2) < 0) child_fail();
                    if (nul > 2) close(nul);
                }
            }
            // 只关 >2 的原 fd：万一某个管道端正好是 0/1/2，dup2 之后它就是标准流，不能关
            if (in_p[0] > 2) close(in_p[0]);
            if (in_p[1] > 2) close(in_p[1]);
            if (out_p[0] > 2) close(out_p[0]);
            if (out_p[1] > 2) close(out_p[1]);
            if (need_err) {
                if (err_p[0] > 2) close(err_p[0]);
                if (err_p[1] > 2) close(err_p[1]);
            }
            if (exec_p[0] > 2) close(exec_p[0]);
            execve(argv_c[0], argv_c, environ);
            child_fail();
        }

        // 父进程
        pid = child;
        setpgid(pid, pid);  // 和子进程里的 setpgid 竞争；谁先都行
        close(in_p[0]);
        close(out_p[1]);
        if (need_err) close(err_p[1]);
        close(exec_p[1]);

        int child_errno = 0;
        ssize_t n;
        do {
            n = read(exec_p[0], &child_errno, sizeof child_errno);
        } while (n < 0 && errno == EINTR);
        close(exec_p[0]);
        if (n > 0) {
            close(in_p[1]);
            close(out_p[0]);
            if (need_err) {
                close(err_p[0]);
                close(err_file);
            }
            int st = 0;
            while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {
            }
            start_failed("启动 " + av[0] + " 失败：" + err_str(child_errno));
            pid = -1;
            reaped = true;
            return false;
        }

        in_w = in_p[1];
        const int fl = fcntl(in_w, F_GETFL);
        if (fl >= 0) fcntl(in_w, F_SETFL, fl | O_NONBLOCK);
        cpu->pid = pid;

        out = std::make_shared<OutState>();
        out->fd = out_p[0];
        out->pid = pid;
        out->note = note;
        out->cpu = cpu;
        try {
            rd_thread = std::thread(out_reader, out);
            rd_started = true;
        } catch (...) {
            fatal("建读线程失败");
        }
        if (need_err) {
            err = std::make_shared<ErrState>();
            err->fd = err_p[0];
            err->out = err_file;
            try {
                er_thread = std::thread(err_reader, err);
                er_started = true;
            } catch (...) {
                fatal("建读线程失败");
            }
        }
        mark = 0;
        send(header);  // 开局信息很短，管道肯定放得下
        return true;
    }

    // 写给选手（不阻塞）。返回 false = 管道满了（选手不读输入）
    bool send(const std::string& text) {
        if (in_w < 0) return true;
        size_t done = 0;
        while (done < text.size()) {
            const ssize_t w = write(in_w, text.data() + done, text.size() - done);
            if (w < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) return false;
                return true;  // 对方已关闭输入：等读线程发现退出
            }
            done += size_t(w);
        }
        return true;
    }

    void kill_tree() {
        if (pid <= 0) return;
        kill(-pid, SIGKILL);
        kill(pid, SIGKILL);
    }

    void kill_now(Status st, i64 t, const std::string& kind, const std::string& detail) {
        dead = true;
        status = st;
        dead_step = t;
        waiting = false;
        faults.push_back({t, kind, detail});
        kill_tree();
    }

    bool flooded(std::string& why) {
        if (!out) return false;
        std::lock_guard<std::mutex> lk(out->mu);
        why = out->flood_why;
        return out->flood;
    }

    void ask(const std::string& obs, i64 t) {
        std::string why;
        if (!dead && flooded(why)) kill_now(ST_OUTPUT_LIMIT, t, "output_limit", why);
        waiting = !dead;
        asked_at = now_ms();
        if (waiting && !send(obs)) kill_now(ST_LOST, t, "lost", "输入管道已满（程序没有读输入）");
    }

    // 0 = 还在等；1 = 本步有结论（act 已填）
    int poll(i64 t, soccer::Action& act) {
        if (!waiting) {
            act.ok = false;
            return 1;
        }
        const double limit = t == 0 ? g_lim.first_ms : g_lim.step_ms;
        const double deadline = asked_at + (t == 0 ? g_lim.first_wall_ms : g_lim.wall_ms);
        std::unique_lock<std::mutex> lk(out->mu);
        if (out->flood) {
            const std::string why = out->flood_why;
            lk.unlock();
            kill_now(ST_OUTPUT_LIMIT, t, "output_limit", why);
            act.ok = false;
            return 1;
        }
        if (!out->lines.empty()) {
            const Line ln = out->lines.front();
            out->lines.pop_front();
            lk.unlock();
            judge(t, ln, limit, deadline, act);
            return 1;
        }
        const bool eof = out->eof;
        lk.unlock();
        if (eof) {
            const bool exited = reap_nonblock();
            kill_now(ST_CRASH, t, "crash",
                     exited ? exit_desc() : "关闭了标准输出（进程仍在运行，已结束）");
            act.ok = false;
            return 1;
        }
        const double now = now_ms();
        if (now > deadline) {
            if (reap_nonblock()) {
                // 主进程已退出，但标准输出还被别的进程（它开的子进程）占着、没有结束标志：按崩溃 / 退出处理
                kill_now(ST_CRASH, t, "crash", exit_desc() + "；标准输出仍被其他进程占着，已结束整个进程组");
                act.ok = false;
                return 1;
            }
            const double ms = cpu->ms() - mark;
            kill_now(ST_LOST, t, "lost",
                     "墙上时钟 " + num2(now - asked_at) + " ms 没有回复（CPU " + num2(ms) + " ms）");
            act.ok = false;
            return 1;
        }
        return 0;
    }

    // 判一行回复。截止时刻以"读线程读到这一行的时刻"为准：截止前到达的照常收（哪怕裁判稍后才处理），截止后到达的判失联
    void judge(i64 t, const Line& ln, double limit, double deadline, soccer::Action& act) {
        waiting = false;
        if (ln.recv_ms > deadline) {
            const double ms = ln.cpu_ms - mark;
            kill_now(ST_LOST, t, "lost",
                     "回复在截止之后才到（墙上时钟 " + num2(ln.recv_ms - asked_at) + " ms，CPU " + num2(ms) + " ms）");
            act.ok = false;
            return;
        }
        const double ms = ln.cpu_ms - mark;
        mark = ln.cpu_ms;
        if (t == 0) first_ms = ms;
        else {
            sum_ms += ms;
            if (ms > max_ms) max_ms = ms;
            ++counted;
        }
        if (ms > limit) {
            ++timeouts;
            act.ok = false;
            std::string d = num2(ms) + " ms";
            if (!ln.too_long) d += "；原始回复 \"" + safe_raw(ln.text) + "\"";
            faults.push_back({t, "timeout", d});
            if (g_lim.max_timeouts > 0 && timeouts >= g_lim.max_timeouts)
                kill_now(ST_TIMEOUT_LIMIT, t + 1, "timeout_limit", "累计超时 " + std::to_string(timeouts) + " 次");
            return;
        }
        if (ln.too_long) {
            act.ok = false;
            ++invalid;
            faults.push_back({t, "invalid", "一行太长（" + std::to_string(ln.len) + " 字节）"});
            return;
        }
        act = soccer::parse_action(ln.text);
        if (!act.ok) {
            ++invalid;
            faults.push_back({t, "invalid", safe_raw(ln.text, 60)});
        }
    }

    // 收尾：关输入、给 0.5 秒自己退出，然后结束整个进程组；读线程有限等待。所有等待都有上限
    void finish() {
        if (finished) return;
        finished = true;
        if (in_w >= 0) {
            close(in_w);
            in_w = -1;
        }
        if (pid > 0 && !dead) {
            const double t0 = now_ms();
            while (!reap_nonblock() && now_ms() - t0 < 500)
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        if (cpu) total_cpu_ms = cpu->ms();
        kill_tree();
        if (pid > 0 && !reaped) {
            const double t0 = now_ms();
            while (!reap_nonblock() && now_ms() - t0 < 1000)
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        if (cpu) {
            total_cpu_ms = cpu->ms();
            job_cpu_ms = total_cpu_ms;  // Unix 版没有单独的 Job 记账
        }
        if (out) out->stop = true;
        if (err) err->stop = true;
        if (rd_started && rd_thread.joinable()) rd_thread.join();
        if (er_started && er_thread.joinable()) er_thread.join();
        pid = -1;
    }
};

// -------------------------------------------------------------------- 一局

struct Job {
    int id = 0;
    unsigned long long seed = 1;
    int first_kick = 0;
    std::string path[4];
    std::string cmdline[4];
    std::string name[4];
    std::string replay;  // 空 = 不写回放
};

std::string fmt_event(const soccer::Event& e) {
    using namespace soccer;
    char b[200];
    switch (e.type) {
        case EV_KICKOFF: std::snprintf(b, sizeof b, "EV %lld kickoff %lld %lld %lld", e.t, e.a, e.b, e.c); break;
        case EV_KICK:
            std::snprintf(b, sizeof b, "EV %lld kick %lld %lld %lld %lld %lld %lld", e.t, e.a, e.b, e.c, e.d, e.e, e.f);
            break;
        case EV_GOAL:
            std::snprintf(b, sizeof b, "EV %lld goal %lld %lld %lld %lld %lld", e.t, e.a, e.b, e.c, e.d, e.e);
            break;
        case EV_STALL: std::snprintf(b, sizeof b, "EV %lld stall %lld", e.t, e.a); break;
        default: return "";
    }
    return b;
}

std::string state_line(const soccer::Game& g) {
    char b[400];
    int n = std::snprintf(b, sizeof b, "S %lld %lld %lld %lld %lld %lld %lld", g.t, g.score[0], g.score[1], g.bx, g.by,
                          g.bvx, g.bvy);
    for (int p = 0; p < 4; ++p)
        n += std::snprintf(b + n, sizeof b - size_t(n), " %lld %lld %lld %lld %lld", g.px[p], g.py[p], g.pvx[p], g.pvy[p],
                           g.cd[p]);
    return std::string(b, size_t(n));
}

std::string play_game(const Job& job) {
    soccer::Game g(g_cfg, job.seed, job.first_kick, true);
    auto note = std::make_shared<Notifier>();
    std::vector<std::unique_ptr<Player>> pl;
    for (int p = 0; p < 4; ++p) {
        pl.emplace_back(new Player());
        Player& P = *pl.back();
        P.path = job.path[p];
        P.cmdline = job.cmdline[p];
        P.slot = p;
        P.game = job.id;
    }
    const bool tdbg = std::getenv("ARENA_DEBUG_TIMING") != nullptr;  // 调试：在标准错误打各阶段墙钟
    const double t_start0 = now_ms();
    for (int p = 0; p < 4; ++p)
        pl[size_t(p)]->start(soccer::protocol_header(g_cfg, p % 2, (i64)g_lim.step_ms, (i64)g_lim.first_ms), note);
    const double t_start1 = now_ms();

    std::string rep;
    const bool want_replay = !job.replay.empty();
    if (want_replay) {
        rep.reserve(1 << 19);
        rep += "SOCCER2V2-REPLAY 2\nCFG " + g_cfg.cfg_line() + "\nSEED " + std::to_string(job.seed) + "\nFIRST_KICK " +
               std::to_string(job.first_kick) + "\n";
        for (int p = 0; p < 4; ++p)
            rep += "PLAYER " + std::to_string(p) + " " + (p < 2 ? "A" : "B") + " " + std::to_string(p % 2 + 1) + " " +
                   utf8_fix(job.name[p]) + "\n";
        rep += "LIMITS " + num2(g_lim.step_ms) + " " + num2(g_lim.first_ms) + "\n";
        rep += "STEPS " + std::to_string(g_cfg.steps) + "\n";
    }
    size_t ev_done = 0;
    if (want_replay) {
        for (; ev_done < g.events.size(); ++ev_done) {
            const std::string s = fmt_event(g.events[ev_done]);
            if (!s.empty()) rep += s + "\n";
        }
    }
    size_t fault_done[4] = {0, 0, 0, 0};
    while (!g.over) {
        const i64 t = g.t;
        if (want_replay) rep += state_line(g) + "\n";
        soccer::Action acts[4];
        for (int p = 0; p < 4; ++p) pl[size_t(p)]->ask(soccer::protocol_step(g.observe(p)), t);
        bool done[4] = {false, false, false, false};
        for (;;) {
            bool all = true;
            for (int p = 0; p < 4; ++p)
                if (!done[p]) {
                    if (pl[size_t(p)]->poll(t, acts[p])) done[p] = true;
                    else all = false;
                }
            if (all) break;
            std::unique_lock<std::mutex> lk(note->mu);
            note->cv.wait_for(lk, std::chrono::milliseconds(5));
        }
        // 记下实际执行的动作（各自视角，不合法 → 0 0 0 0 0；越界消息 → 0）
        i64 shown[4][5];
        for (int p = 0; p < 4; ++p) {
            const soccer::Action& a = acts[p];
            const bool ok = a.ok && soccer::Game::action_in_range(a);
            i64 m = ok ? a.msg : 0;
            if (g_cfg.msg_max < 0 || m < 0 || m > g_cfg.msg_max) m = 0;
            shown[p][0] = ok ? a.ax : 0;
            shown[p][1] = ok ? a.ay : 0;
            shown[p][2] = ok ? a.kx : 0;
            shown[p][3] = ok ? a.ky : 0;
            shown[p][4] = m;
        }
        g.step(acts);
        if (want_replay) {
            std::string ln = std::to_string(t);
            for (int p = 0; p < 4; ++p) {
                ln += " |";
                for (int k = 0; k < 5; ++k) ln += " " + std::to_string(shown[p][k]);
            }
            rep += ln + "\n";
            for (; ev_done < g.events.size(); ++ev_done) {
                const std::string s = fmt_event(g.events[ev_done]);
                if (!s.empty()) rep += s + "\n";
            }
            for (int p = 0; p < 4; ++p) {
                auto& F = pl[size_t(p)]->faults;
                for (; fault_done[p] < F.size(); ++fault_done[p]) {
                    std::string d = utf8_fix(F[fault_done[p]].detail);
                    for (char& c : d)
                        if (c == '\n' || c == '\r') c = ' ';
                    rep += "F " + std::to_string(F[fault_done[p]].t) + " " + std::to_string(p) + " " +
                           F[fault_done[p]].kind + " " + d + "\n";
                }
            }
        }
    }
    const double t_play1 = now_ms();
    for (auto& P : pl) P->finish();
    if (tdbg)
        std::fprintf(stderr, "arena 计时：启动 4 人 %.1f ms，比赛 %.1f ms，收尾 %.1f ms\n", t_start1 - t_start0,
                     t_play1 - t_start1, now_ms() - t_play1);
    if (want_replay) {
        // 收尾时才发现的故障（例如最后一步之后才报的输出超量）不写；故障都在对应步之后写过了
        rep += state_line(g) + "\n";
        rep += "END " + std::to_string(g.score[0]) + " " + std::to_string(g.score[1]) + " " + g.digest() + "\n";
        const std::string e = write_file_atomic(job.replay, rep);
        if (!e.empty()) fatal("写不了回放文件 " + job.replay + "：" + e);
    }

    std::string o = "{\"id\":" + std::to_string(job.id) + ",\"seed\":" + std::to_string(job.seed) +
                    ",\"first_kick\":" + std::to_string(job.first_kick) + ",\"score\":[" + std::to_string(g.score[0]) +
                    "," + std::to_string(g.score[1]) + "],\"steps\":" + std::to_string(g.t) + ",\"digest\":\"" +
                    g.digest() + "\",\"stalls\":" + std::to_string(g.stalls) + ",\"goals\":[";
    for (size_t i = 0; i < g.goals.size(); ++i)
        o += (i ? ",[" : "[") + std::to_string(g.goals[i].first) + "," + std::to_string(g.goals[i].second) + "]";
    o += "],\"players\":[";
    for (int p = 0; p < 4; ++p) {
        Player& P = *pl[size_t(p)];
        o += std::string(p ? "," : "") + "{\"name\":" + json_str(job.name[p]) + ",\"path\":" + json_str(job.path[p]) +
             ",\"kicks\":" + std::to_string(g.kicks[p]) + ",\"status\":\"" + STATUS_NAME[P.status] +
             "\",\"dead_step\":" + std::to_string(P.dead_step) + ",\"timeouts\":" + std::to_string(P.timeouts) +
             ",\"invalid\":" + std::to_string(P.invalid) + ",\"first_ms\":" + num2(P.first_ms) +
             ",\"avg_ms\":" + num2(P.counted ? P.sum_ms / P.counted : 0) + ",\"max_ms\":" + num2(P.max_ms) +
             ",\"cpu_total_ms\":" + num2(P.total_cpu_ms) + ",\"job_cpu_ms\":" + num2(P.job_cpu_ms) +
             ",\"sandbox\":false,\"faults\":[";
        const size_t nf = P.faults.size() < 20 ? P.faults.size() : 20;
        for (size_t i = 0; i < nf; ++i)
            o += std::string(i ? "," : "") + "[" + std::to_string(P.faults[i].t) + "," + json_str(P.faults[i].kind) + "," +
                 json_str(P.faults[i].detail) + "]";
        o += "],\"faults_total\":" + std::to_string(P.faults.size()) + "}";
    }
    o += "]";
    if (want_replay) o += ",\"replay\":" + json_str(job.replay);
    o += "}";
    return o;
}

// -------------------------------------------------------------------- 命令行

struct Args {
    std::vector<std::string> u;  // UTF-8
};

// 解析共同选项；返回 false 表示 i 处不是共同选项
bool common_opt(const Args& a, size_t& i) {
    const std::string& k = a.u[i];
    if (i + 1 >= a.u.size()) return false;
    const std::string& v = a.u[i + 1];
    if (k == "--step-ms") g_lim.step_ms = std::atof(v.c_str());
    else if (k == "--first-ms") g_lim.first_ms = std::atof(v.c_str());
    else if (k == "--wall-ms") g_lim.wall_ms = std::atof(v.c_str());
    else if (k == "--first-wall-ms") g_lim.first_wall_ms = std::atof(v.c_str());
    else if (k == "--max-timeouts") g_lim.max_timeouts = std::atoi(v.c_str());
    else if (k == "--stderr-dir") g_stderr_dir = v;
    else if (k == "--replay-dir") g_replay_dir = v;
    else if (k == "--mem-mb" || k == "--max-procs" || k == "--sandbox") {
        // 兼容旧调用；Unix 版不内置沙箱和资源限制
        std::fprintf(stderr, "arena：Unix 版不内置沙箱，忽略 %s\n", k.c_str());
    } else if (k == "--cfg") {
        const size_t e = v.find('=');
        if (e == std::string::npos || !g_cfg.set(v.substr(0, e), v.substr(e + 1))) fatal("--cfg 参数不对：" + v);
    } else return false;
    ++i;
    return true;
}

int usage() {
    std::fprintf(stderr,
                 "用法：\n"
                 "  arena play <A1> <A2> <B1> <B2> [--seed S] [--first-kick 0|1] [--names a,b,c,d]\n"
                 "             [--replay 文件] [--json 文件] [共同选项]\n"
                 "  arena batch [--workers K] [--replay-dir 目录] [共同选项] < 任务\n"
                 "  arena calib\n"
                 "共同选项：--step-ms 10 --first-ms 1000 --wall-ms 1000 --first-wall-ms 5000 --max-timeouts 30\n"
                 "          --stderr-dir 目录 --cfg KEY=VAL\n"
                 "Unix 版不内置沙箱；--mem-mb / --max-procs / --sandbox 仍接受但被忽略。\n"
                 "详见 RULES.md / README.md\n");
    return 2;
}

int cmd_play(const Args& a) {
    Job job;
    std::vector<std::string> exes;
    std::string json_path;
    std::string names;
    for (size_t i = 2; i < a.u.size(); ++i) {
        const std::string& k = a.u[i];
        if (common_opt(a, i)) continue;
        if (k == "--seed" && i + 1 < a.u.size()) job.seed = std::strtoull(a.u[++i].c_str(), nullptr, 10);
        else if (k == "--first-kick" && i + 1 < a.u.size()) job.first_kick = std::atoi(a.u[++i].c_str()) ? 1 : 0;
        else if (k == "--names" && i + 1 < a.u.size()) names = a.u[++i];
        else if (k == "--replay" && i + 1 < a.u.size()) job.replay = a.u[++i];
        else if (k == "--json" && i + 1 < a.u.size()) json_path = a.u[++i];
        else if (k.rfind("--", 0) == 0) return usage();
        else exes.push_back(a.u[i]);
    }
    if (exes.size() != 4) return usage();
    std::vector<std::string> nm;
    {
        size_t s = 0;
        while (!names.empty()) {
            const size_t e = names.find(',', s);
            nm.push_back(names.substr(s, e == std::string::npos ? std::string::npos : e - s));
            if (e == std::string::npos) break;
            s = e + 1;
        }
    }
    for (int p = 0; p < 4; ++p) {
        job.path[p] = absolute_path(exes[size_t(p)]);
        if (size_t(p) < nm.size() && !nm[size_t(p)].empty()) job.name[p] = nm[size_t(p)];
        else {
            std::string s = exes[size_t(p)];
            const size_t c = s.find_last_of('/');
            if (c != std::string::npos) s = s.substr(c + 1);
            job.name[p] = utf8_fix(s);
        }
        for (char& c : job.name[p])
            if (c == ' ' || c == '\t') c = '_';
    }
    if (!g_stderr_dir.empty()) ensure_dir(g_stderr_dir);
    const std::string res = play_game(job);
    if (!json_path.empty()) {
        const std::string e = write_file_atomic(json_path, res + "\n");
        if (!e.empty()) fatal("写不了结果文件 " + json_path + "：" + e);
    } else {
        emit(res);
    }
    return 0;
}

int cmd_batch(const Args& a) {
    unsigned hw = std::thread::hardware_concurrency();
    int workers = int(hw) / 2;
    if (workers < 1) workers = 1;
    for (size_t i = 2; i < a.u.size(); ++i) {
        if (common_opt(a, i)) continue;
        if (a.u[i] == "--workers" && i + 1 < a.u.size()) workers = std::atoi(a.u[++i].c_str());
        else return usage();
    }
    if (workers < 1) workers = 1;
    if (workers > 64) workers = 64;
    struct Bot {
        std::string name;
        std::string path;
        std::string cmdline;
    };
    std::vector<Bot> bots;
    std::vector<Job> jobs;
    char buf[8192];
    while (std::fgets(buf, sizeof buf, stdin)) {
        std::string l(buf);
        while (!l.empty() && (l.back() == '\n' || l.back() == '\r')) l.pop_back();
        if (l.rfind("BOT ", 0) == 0) {
            int idx = 0, off = 0;
            char name[512];
            if (std::sscanf(l.c_str(), "BOT %d %511s %n", &idx, name, &off) < 2 || off == 0 || idx < 0 || idx > 100000)
                fatal("BOT 行读不懂：" + safe_raw(l, 200));
            if (int(bots.size()) <= idx) bots.resize(size_t(idx) + 1);
            bots[size_t(idx)] = {utf8_fix(name), absolute_path(l.substr(size_t(off))), ""};
        } else if (l.rfind("BOTCMD ", 0) == 0) {
            int idx = 0, off = 0;
            char name[512];
            if (std::sscanf(l.c_str(), "BOTCMD %d %511s %n", &idx, name, &off) < 2 || off == 0 || idx < 0 ||
                idx > 100000)
                fatal("BOTCMD 行读不懂：" + safe_raw(l, 200));
            if (int(bots.size()) <= idx) bots.resize(size_t(idx) + 1);
            const std::string c = l.substr(size_t(off));
            bots[size_t(idx)] = {utf8_fix(name), c, c};
        } else if (l.rfind("GAME ", 0) == 0) {
            Job j;
            int b[4];
            if (std::sscanf(l.c_str(), "GAME %d %llu %d %d %d %d %d", &j.id, &j.seed, &j.first_kick, &b[0], &b[1],
                            &b[2], &b[3]) != 7)
                fatal("GAME 行读不懂：" + safe_raw(l, 200));
            j.first_kick = j.first_kick ? 1 : 0;
            for (int p = 0; p < 4; ++p) {
                if (b[p] < 0 || b[p] >= int(bots.size()) || bots[size_t(b[p])].path.empty())
                    fatal("GAME 行的 bot 编号不对：" + safe_raw(l, 200));
                j.path[p] = bots[size_t(b[p])].path;
                j.cmdline[p] = bots[size_t(b[p])].cmdline;
                j.name[p] = bots[size_t(b[p])].name;
            }
            if (!g_replay_dir.empty()) j.replay = join_path(g_replay_dir, "g" + std::to_string(j.id) + ".txt");
            jobs.push_back(j);
        } else if (!l.empty() && l[0] != '#') {
            fatal("任务行读不懂：" + safe_raw(l, 200));
        }
    }
    if (!g_replay_dir.empty()) ensure_dir(g_replay_dir);
    if (!g_stderr_dir.empty()) ensure_dir(g_stderr_dir);
    std::atomic<size_t> next{0};
    std::vector<std::thread> th;
    for (int w = 0; w < workers; ++w)
        th.emplace_back([&] {
            for (;;) {
                const size_t i = next.fetch_add(1);
                if (i >= jobs.size()) break;
                emit(play_game(jobs[i]));
            }
        });
    for (auto& t : th) t.join();
    return 0;
}

void raise_stack_limit() {
    struct rlimit rl {};
    if (getrlimit(RLIMIT_STACK, &rl) != 0) return;
    const rlim_t want = rlim_t(64) << 20;  // 与 Windows 版相同的 64 MB
    if (rl.rlim_cur != RLIM_INFINITY && rl.rlim_cur < want) {
        rl.rlim_cur = (rl.rlim_max == RLIM_INFINITY || rl.rlim_max >= want) ? want : rl.rlim_max;
        setrlimit(RLIMIT_STACK, &rl);  // 失败就算了；选手程序自己也不该依赖大栈
    }
}

}  // namespace

int main(int argc, char** argv) {
    signal(SIGPIPE, SIG_IGN);
    raise_stack_limit();
    Args a;
    a.u.reserve(size_t(argc));
    for (int i = 0; i < argc; ++i) a.u.emplace_back(argv[i]);
    if (a.u.size() < 2) return usage();
    const std::string cmd = a.u[1];
    if (cmd == "calib") {
        // Unix 版直接按 CPU 毫秒记账，不再做周期换算；保留此命令只为兼容旧脚本。
        std::printf("1\n");
        return 0;
    }
    if (cmd == "play" || cmd == "batch") return cmd == "play" ? cmd_play(a) : cmd_batch(a);
    return usage();
}
