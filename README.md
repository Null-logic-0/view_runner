# browser-automation-lab

An educational lab for measuring what actually happens when you automate a browser:
how long a session costs, where a proxy fails, and what concurrency really buys you.

It runs N browser sessions against a target you control, optionally through proxies you
control, records a row of data per session, and reports what it measured.

```bash
automation-lab doctor
automation-lab run --url http://127.0.0.1:8000/ -n 10 -d 30 -j 5
```

---

## What this is, in plain English

A browser is a big, expensive program. This project opens one, points it at a web page, holds
it there for a while, closes it, and writes down exactly how long each part took and whether
anything went wrong. Then it does that again — ten times, or fifty, sometimes several at once —
and gives you a report.

That's it. It's a stopwatch and a notebook wrapped around a web browser.

The interesting questions turn out to be things like: how much does opening a browser tab
actually cost? Does running ten at once make them each slower, and by how much? If you send the
traffic through a proxy, what does that add? When something fails, *what* failed — the proxy,
the network, or the page itself? Those have real answers, and most people guess instead of
measuring.

It comes with its own practice website and its own proxy server, so you can break things on
purpose and watch what happens.

---

## Why I built this

This started as a rewrite of an old "YouTube view bot" I found — 95 lines that opened Chrome
through a random proxy, waited, and closed it. I didn't want a working view bot. I wanted to
understand every layer that little script was skating over, and the honest way to do that was
to rebuild it properly against a target I own.

The things I set out to learn, and where each one lives in this repo:

| What I wanted to understand | Where it shows up |
| --- | --- |
| Modern Python project structure | `pyproject.toml`, `uv.lock`, one dependency |
| How browser automation actually works | `app/browser/` — driver, protocol, context, page |
| HTTP and proxies, underneath the libraries | `lab/proxy_server.py`, written from scratch |
| asyncio: coroutines, tasks, cancellation | `app/runner.py` — semaphore, TaskGroup, `except*` |
| Not leaking resources when things fail | `try/finally` everywhere, with tests that prove it |
| Testing things that are slow and stateful | 306 tests; 269 of them need no browser at all |
| Observability that isn't `print()` | `app/telemetry/` — structured logs, JSONL results |
| Designing so pieces can change independently | `runner.py` cannot import Playwright, by test |
| Measuring performance without fooling myself | [Concurrency](#concurrency) and the experiments |

The last one turned out to be the hardest and the most useful. It is very easy to produce a
table of numbers that looks authoritative and means nothing. Most of this project's design —
recording every session whether it succeeded or not, keeping the config next to the results,
refusing to average a "time to succeed" together with a "time to fail" — exists because of
that, not because of anything to do with browsers.

**What I'd tell someone starting the same thing:** almost every real problem here was found by
running the code, not by reading it. A port Chromium silently refuses to connect to. A shutdown
that took half a second for no reason. A deadlock from calling a blocking function inside an
event loop. A test double that could never be cancelled because it never waited for anything.
None of those were visible in review. All of them were obvious within seconds of something
actually running.

---

## Table of contents

- [Why I built this](#why-i-built-this)
- [Purpose and scope](#purpose-and-scope)
- [How to use it](#how-to-use-it)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Proxy format](#proxy-format)
- [The lab bench](#the-lab-bench)
- [Running an experiment](#running-an-experiment)
- [Concurrency](#concurrency)
- [Metrics and output](#metrics-and-output)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Architecture decisions](#architecture-decisions)
- [Limitations](#limitations)

---

## Purpose and scope

This is a rewrite of an old "YouTube view bot" as a browser-automation research project.
The mechanics that were worth keeping — proxy handling, browser lifecycle, session timing,
concurrency, observability — all transfer. The part that made the original what it was did
not, and is deliberately absent:

**Not implemented, and not wanted here:** randomised User-Agents, fingerprint spoofing
(`selenium-stealth`), `--disable-blink-features=AutomationControlled`, synthetic "human-like"
mouse movement, or any other mechanism whose purpose is to make automated traffic
indistinguishable from a person so that a platform's abuse controls count it. There is no
CAPTCHA handling and no detection evasion.

Against a target you own, you *want* your automation identifiable. A fixed, honest
`user_agent` makes your own access logs readable and lets you filter lab traffic out of your
analytics. That is why `configs/example.toml` ships with
`user_agent = "browser-automation-lab/0.1.0"`.

**Point this only at targets you own or are authorised to test.**

---

## How to use it

A walkthrough from nothing to your first measurement. No prior knowledge of the codebase needed.

### 1. Set it up

```bash
uv sync                             # installs Python dependencies
uv run playwright install chromium  # downloads the browser itself (~95 MB, separate step)
uv run automation-lab doctor        # checks it all actually works
```

`doctor` prints a checklist. If something is wrong it tells you which thing and, usually, how to
fix it. Do not skip it — the second command above is the one everybody forgets.

### 2. Start the practice lab

You need something to point the browser at. The project ships with one:

```bash
python -m lab.bench --proxies 3 --write-proxy-file proxies.local.txt
```

That starts a small website on `http://127.0.0.1:8000` and three proxy servers. Leave it running
and open a second terminal. The website has pages that misbehave on demand — one that's slow,
one that returns errors, one that fails the first few times you ask — so you can test how your
setup handles trouble without waiting for real trouble.

### 3. Run something

```bash
automation-lab run --url http://127.0.0.1:8000/ -n 5 -d 3
```

That means: open the page **5** times, hold each one open for **3** seconds. It will take about
15 seconds and then print a report.

### 4. Read the report

```
Sessions:                 5      how many we tried to run
Completed:                5      how many finished properly
Failed:                   0      how many didn't

Average setup:        102.1 ms   opening a fresh, isolated browser tab
Average navigation:     9.3 ms   from "go" until the page had finished loading
Average dwell:     3,000.2 ms    how long we held it open (you asked for 3,000)
Average duration:  3,142.1 ms    the whole session, start to finish

Success rate:         100.0%
Sessions via proxy:        0     none, because we didn't ask for any
Wall clock:             15.7 s   real time you waited
```

Two things worth knowing about these numbers:

- **Averages only cover sessions that worked.** A session that died halfway has no "navigation
  time", and counting it as zero would make a broken run look fast.
- **`Wall clock` is real elapsed time.** Five 3-second sessions one after another take about 15
  seconds. Run them at the same time and that drops — which is the next step.

### 5. Run several at once

```bash
automation-lab run --url http://127.0.0.1:8000/ -n 5 -d 3 -j 5
```

`-j 5` means *five at a time*. Same work, now about 3 seconds of wall clock instead of 15.

Watch the `Average navigation` figure as you raise `-j`. It goes **up**. That isn't the website
getting slower — it's your own machine doing five things at once. This matters: if you compare
speed between two runs at different `-j` values, you are measuring your laptop, not the site.

### 6. Send it through the proxies

```bash
automation-lab run --url http://127.0.0.1:8000/ -n 6 -d 2 -j 3 --proxy-file proxies.local.txt
```

Each session now goes through one of the three proxies, in rotation. `Sessions via proxy` should
read 6.

### 7. Break something on purpose

Stop the bench (Ctrl-C) and restart it with proxies that fail every second request:

```bash
python -m lab.bench --proxies 3 --fail-every 2 --write-proxy-file proxies.local.txt
```

Run the same command as step 6. Six sessions spread across three proxies is two requests each,
so each proxy fails its second one — three failures:

```
Sessions:                 6
Completed:                3
Failed:                   3
  failed_status           3

Proxy attribution
  unreachable (certain):  0
  any failure via proxy:  3
  3 failure(s) cannot be attributed to proxy or target
```

*(The counter is per proxy and cumulative for as long as the bench runs, so restart it if you
want the same result twice. `--fail-every 3` with only six sessions produces no failures at all
— two requests per proxy never reaches the third.)*

Now turn retries on and run it again:

```bash
automation-lab run --url http://127.0.0.1:8000/ -n 6 -d 2 -j 3 \
    --proxy-file proxies.local.txt -c configs/experiments/d2.toml
```

Every session now succeeds — 50% becomes 100%, with nothing fixed. That's worth sitting with: **"success rate" is
not a property of the system on its own, it's a property of the system plus how hard you retry.**
Quoting one without the other is misleading, which is why retries are off by default here.

### 8. Look at what was saved

Every run writes two files into `results/`:

```bash
ls results/
cat results/*.jsonl | head -1     # one line per session
cat results/*.summary.json        # totals, plus the exact settings used
```

The `.jsonl` file has one line per session, so you can ask questions later without re-running
anything:

```bash
# which sessions failed, and why?
cat results/*.jsonl | jq -r 'select(.status != "completed") | "\(.session_id) \(.error_message)"'

# how many sessions did each proxy handle?
cat results/*.jsonl | jq -r .proxy_label | sort | uniq -c
```

The `.summary.json` stores the configuration and the machine alongside the numbers, so in three
weeks you can still answer "what settings produced this?" without trusting your memory.

### 9. Use a config file instead of flags

Once you're running the same thing repeatedly, put it in a file:

```bash
cp configs/example.toml configs/local.toml    # then edit it
automation-lab run -c configs/local.toml
automation-lab config -c configs/local.toml   # shows exactly what will be used
```

Flags still work and override the file, so `-n 2` is handy for a quick check without editing
anything. `configs/local*.toml` is gitignored, so it's the right place for anything you'd rather
not commit.

`configs/experiments/` holds ready-made experiments — see [Concurrency](#concurrency) for what
they found.

### 10. Point it at your own site

```bash
automation-lab run --url https://your-own-site.example/ -n 5 -d 10
```

Only sites you own or are authorised to test. Set `user_agent` in your config to something
identifiable so your own access logs are readable and you can filter lab traffic out of your
analytics.

### If something goes wrong

Run `automation-lab doctor` first — it checks Python, Playwright, the browser, your config, your
proxy file, the results directory, and whether the target responds. [Troubleshooting](#troubleshooting)
covers the specific errors you're most likely to hit.

---

## How it works

Before the abstractions, the layers. When a session calls `page.goto(url)`:

```mermaid
flowchart TD
    A["Your Python: await page.goto(url)"] --> B["Playwright Python API"]
    B --> C["Playwright driver<br/>(bundled Node process, stdio)"]
    C --> D["Chrome DevTools Protocol<br/>(persistent bidirectional WebSocket)"]
    D --> E["Chromium process"]
    E --> F["BrowserContext<br/>isolated cookies · cache · storage · own proxy"]
    F --> G["Page (tab)"]
    G --> H["HTTP / TLS"]
    H --> I["Proxy (optional)"]
    I --> J["Target website"]
```

Two things in that stack explain most of the design:

**The `BrowserContext` is the unit of isolation, not the browser process.** A context has its
own cookie jar, cache, storage — and its own proxy. Measured on an M-series Mac: a browser
launch costs ~110 ms, a context ~1.7 ms. **65× cheaper.** Ten concurrent sessions are one
browser process with ten contexts, not ten browsers.

**The proxy is a per-context setting.** This is the single reason this project uses Playwright
rather than Selenium — see [Architecture decisions](#architecture-decisions).

### One session's lifecycle

```mermaid
flowchart LR
    A[CREATE<br/>context] --> B[START<br/>page]
    B --> C[NAVIGATE<br/>goto]
    C --> D{status ==<br/>expected?}
    D -- no --> F[FAILED_STATUS]
    D -- yes --> E[WAIT<br/>dwell]
    E --> G[COLLECT<br/>timings]
    F --> H[CLOSE<br/>always]
    G --> H
    H --> I[SessionResult]
```

`CLOSE` runs in a `finally` block, so a session that fails anywhere still releases its
context. A session **always** produces a `SessionResult` — success or failure. A failure is a
data point; one that raised would produce no row, and your failure rate would then be computed
only from the sessions that happened to work.

---

## Architecture

```mermaid
flowchart TD
    CLI[cli.py<br/>argv · exit codes]
    EXP[experiment.py<br/>composition root]
    RUN[runner.py<br/>orchestration]
    CFG[config.py<br/>TOML · validation]
    SESS[browser/session.py<br/>one session]
    FACT[browser/factory.py<br/>browser · context]
    POOL[proxy/pool.py<br/>rotation]
    PARSE[proxy/parser.py<br/>Proxy · validation]
    FAIL[failure.py<br/>retry · abort policy]
    TEL[telemetry/<br/>logger · metrics · results]
    ERR[errors.py]

    CLI --> CFG
    CLI --> EXP
    EXP --> RUN
    EXP --> FACT
    EXP --> SESS
    EXP --> POOL
    RUN --> FAIL
    RUN --> TEL
    SESS --> FACT
    SESS --> TEL
    FACT --> PARSE
    POOL --> PARSE
    CFG --> FAIL
    CFG --> TEL
    FAIL --> TEL
    PARSE --> ERR
    CFG --> ERR

    style TEL fill:#e8f4ea,stroke:#2d6a4f
    style ERR fill:#e8f4ea,stroke:#2d6a4f
    style RUN fill:#fdf0e3,stroke:#b45309
```

Three rules hold this together:

1. **`telemetry/` and `errors.py` are leaves** (green). They import nothing else from `app`, so
   everything may import them and there are no cycles. `telemetry.write_summary` takes the
   config as a plain `Mapping` rather than importing `Config`, specifically to preserve this.
2. **`runner.py` never imports Playwright** (amber). Enforced by a test that runs
   `import app.runner` in a subprocess and asserts `playwright not in sys.modules`. This is why
   13 orchestration tests — including every concurrency assertion — run in 0.05 s with a fake
   session.
3. **`experiment.py` is the composition root.** It is the only module allowed to know about
   config, proxies and browsers at once.

| Module | Owns | Does not own |
| --- | --- | --- |
| `config.py` | Parsing, validation, defaults | Reading `proxies.txt`, launching anything |
| `proxy/parser.py` | Entry syntax, host/port validation, the `Proxy` type | Selection order, liveness |
| `proxy/pool.py` | Rotation strategy, cursor | Parsing, connecting, health |
| `browser/factory.py` | Launch args, viewport, locale, proxy wiring, timeouts | Navigation, dwell, metrics |
| `browser/session.py` | One session's lifecycle and measurements | How many sessions, concurrency, which proxy |
| `runner.py` | Count, ordering, proxy assignment, retries, aggregation | What a browser is |
| `failure.py` | Retryability, backoff, abort rule | Doing anything about it |
| `telemetry/` | Log formatting, redaction, metrics, durable output | Deciding what is worth logging |
| `cli.py` | argv, exit codes, error boundary | Any business logic |

---

## Requirements

- **Python 3.12+** (developed on 3.14.6)
- **[uv](https://docs.astral.sh/uv/)** for environment and dependency management
- ~200 MB of disk for the Chromium build Playwright downloads

One runtime dependency: `playwright`. Development adds `pytest`, `pytest-asyncio`, `ruff`,
and `mypy`.

---

## Installation

```bash
uv sync                            # create .venv, install everything from uv.lock
uv run playwright install chromium # download the browser — a SEPARATE step
uv run automation-lab doctor       # verify it all works
```

**`uv sync` installs the Playwright Python client, not a browser.** Chromium is a separate
~95 MB download. Forgetting the second line is the most common setup failure, which is why
`doctor` launches a real browser rather than just checking that the package imports.

`uv.lock` pins the exact resolved version and hash of every package including transitive
dependencies. It is committed on purpose — it is what makes "reproducible on another machine"
true rather than aspirational.

---

## Configuration

Experiments are described by a TOML file, so an experiment is a diffable record rather than
shell history. Copy the example and edit the copy:

```bash
cp configs/example.toml configs/local.toml   # configs/local*.toml is gitignored
```

```toml
[target]
url = "http://127.0.0.1:8000/"
expected_status = 200      # anything else counts the session as failed

[session]
count = 10
duration_seconds = 30      # 0 = navigate then close immediately
wait_until = "load"        # commit | domcontentloaded | load | networkidle

[browser]
engine = "chromium"        # chromium | firefox | webkit
headless = true
viewport_width = 1280
viewport_height = 800
locale = "en-US"
user_agent = "browser-automation-lab/0.1.0"   # empty = browser default

[timeouts]
launch_ms = 30000
navigation_ms = 30000

[proxy]
enabled = false
file = "proxies.txt"
default_scheme = "http"    # http | https | socks5

[runner]
concurrency = 1            # max 64

[telemetry]
level = "INFO"             # DEBUG | INFO | WARNING | ERROR | CRITICAL
format = "text"            # text for humans, json for jq
results_dir = "results"    # empty string disables writing results

[failure]
max_attempts = 1                      # 1 = no retries
retry_backoff_seconds = 0.5           # doubles per attempt
abort_after_consecutive_failures = 0  # 0 = never abort early
```

### Precedence

```
dataclass defaults  <  TOML file  <  CLI flags
```

CLI flags merge into the parsed TOML mapping and are then validated by the **same**
`build_config` as everything else, so `--url not-a-url` gives the identical error a bad file
would. Absent flags are `None`, never a default — so an omitted `--count` cannot silently
overwrite `count = 10` in your file.

Inspect the result of all three layers before running anything:

```bash
automation-lab config -c configs/local.toml -n 2 -j 4
```

### `wait_until` decides what you are measuring

This one setting changes every latency number in every experiment:

| value | `goto()` returns when… | `navigation_ms` measures |
| --- | --- | --- |
| `commit` | the first response bytes arrive | time to first byte, roughly |
| `domcontentloaded` | the HTML is parsed | server + parse |
| `load` *(default)* | the page and its subresources are loaded | the full page load |
| `networkidle` | the network has been quiet ~500 ms | load plus lazy requests |

Pick one, record it, and do not compare numbers taken with different values.

### Unknown keys are errors

A typo like `durations_seconds = 5` is rejected, with a suggestion:

```
2 configuration problems in configs/local.toml:
  - session.durations_seconds is not a known setting; did you mean 'duration_seconds'?
  - runner.concurrency must be <= 64, got 500
```

A silently ignored key means the program runs perfectly and **measures something other than
what the file says** — the worst possible failure for a measurement tool.

---

## Proxy format

`proxies.txt` is one entry per line. Blank lines and `#` comments are skipped.

```
103.229.247.202:37927                  host:port                    ← what proxies.txt uses
http://1.2.3.4:8080                    scheme://host:port
bob:secret@proxy.internal:1080         user:pass@host:port
socks5://bob:secret@1.2.3.4:1080       scheme://user:pass@host:port
[::1]:3128                             IPv6 literals
```

`host:port:user:pass` is **rejected**, deliberately. Given `a:b:c:d` there is no rule that
distinguishes it from a host with a colon-bearing password or a mangled IPv6 literal — any
parser supporting it is guessing silently. Use the `@` form, which says what it means.

Parsing delegates to `urllib.parse` rather than splitting on `":"`, so IPv6 brackets,
percent-encoded credentials (`p%40ss` → `p@ss`) and port range checks are all handled
correctly. A malformed line is **reported and skipped**, not fatal — 12 bad lines out of 6,854
leave 6,842 usable proxies. An empty pool *is* fatal.

```mermaid
flowchart LR
    F["proxies.txt"] --> P["parse_text"]
    P --> R["ParseReport<br/>proxies + errors"]
    R --> POOL["ProxyPool<br/>round-robin"]
    POOL --> S["BrowserContext<br/>per-context proxy"]
```

Rotation is deterministic: with a 3-proxy pool, sessions get `p1, p2, p3, p1, p2, …`, every
run, so an experiment is reproducible. This is unlike the original project's
`random.choice()`, which sampled with replacement and could not be repeated.

---

## The lab bench

`lab/` is the instrument: a target you control and proxies you control. Nothing in `app/`
imports it, so the system under test can never depend on behaviour that only exists in the lab.

```bash
python -m lab.bench --proxies 3 --write-proxy-file proxies.local.txt --access-log access.jsonl
```

```
target   http://127.0.0.1:8000
         /  /slow?ms=N  /status?code=N  /heavy?n=N  /flaky?fail=N  /__headers  /__stats
proxy    127.0.0.1:3128
proxy    127.0.0.1:3129
proxy    127.0.0.1:3130
```

Then, in another shell:

```bash
automation-lab run --url http://127.0.0.1:8000/ -n 9 -d 0.3 -j 3 --proxy-file proxies.local.txt
```

### Why a target you control

Third-party sites are poor instruments — their latency, caching and rate limits drift
independently of your experiment. Owning both ends buys two things you cannot otherwise have:

**Calibration.** `/slow?ms=800` has a known answer. If `navigation_ms` comes back at 40 ms your
timer is measuring the wrong thing — probably `goto()` returning at `commit` rather than
`load`. `test_navigation_timing_tracks_a_delay_we_caused` is exactly this check.

**Verification.** The lab reports 9 completed sessions. Does the server agree?

```
lab telemetry says    : 9 sessions completed
target access log has : 9 requests for /lab-session
target /__stats says  : 9 total requests, 9 via proxy
proxies used          : {3228: 3, 3229: 3, 3230: 3}   <- perfectly even rotation
```

Against a site you cannot see inside, telemetry that lies is indistinguishable from telemetry
that works.

### Failures you cause rather than wait for

| | |
| --- | --- |
| `/slow?ms=N` | navigation latency with a known value |
| `/status?code=N` | exercise `expected_status` and the 5xx-only retry rule |
| `/flaky?fail=N` | first N requests fail, then recovery — **deterministic**, not random |
| `/heavy?n=N` | N subresources, so `wait_until` has something to wait for |
| `/__headers` | echoes what arrived, so you can see what a proxy rewrote |
| `--delay-ms N` | proxy-side latency |
| `--fail-every N` | proxy fails every Nth request with 502 |
| `--auth user:pass` | Basic proxy authentication, so credential redaction is testable |

### The proxy is written from scratch — on purpose

`lab/proxy_server.py` is ~380 lines with no dependencies, because **plain HTTP and HTTPS are
proxied by completely different mechanisms** and that is worth seeing rather than reading about:

```mermaid
sequenceDiagram
    participant C as Client
    participant P as Proxy
    participant T as Target

    Note over C,T: Plain HTTP - the proxy sees everything
    C->>P: GET http://target/page HTTP/1.1
    P->>P: rewrite to origin form, strip hop-by-hop, add Via
    P->>T: GET /page HTTP/1.1
    T-->>P: 200 OK
    P-->>C: 200 OK

    Note over C,T: HTTPS - the proxy sees the hostname and nothing else
    C->>P: CONNECT target:443 HTTP/1.1
    P->>T: opens a TCP connection
    P-->>C: 200 Connection Established
    C->>T: opaque bytes (TLS) relayed blindly
```

Demonstrated by test: after a `CONNECT`, the proxy's log contains `CONNECT 127.0.0.1:61131`
while the target received `/inside`. **The proxy never saw that path.** That is why a proxy can
log your HTTP URLs but not your HTTPS ones, and why a MITM proxy needs you to trust a
certificate it generates.

Deliberate limitations, because this is an instrument and not a product: one request per
connection (`Connection: close` upstream), and request bodies only with `Content-Length`. For
measuring our own client those cost nothing. For production traffic, use tinyproxy or Squid.

---

## Running an experiment

```bash
automation-lab run -c configs/local.toml
automation-lab run --url http://127.0.0.1:8000/ -n 10 -d 30 -j 5
automation-lab run --url http://... --proxy-file proxies.txt --fail-under 0.9
automation-lab run --url http://... --headed         # watch the browser
```

### Exit codes

| code | meaning |
| --- | --- |
| `0` | the run was sound (sessions may have failed — that is data) |
| `1` | it could not run: bad config, no browser, unreadable proxy file |
| `2` | usage error (argparse) |
| `3` | aborted by the failure policy, or `--fail-under` not met |
| `130` | interrupted (128 + SIGINT) |

**Sessions failing exits `0` on purpose.** A lab that fails its own exit code because the thing
it measured was unhealthy cannot be scripted around. Opt into strictness with `--fail-under`.

### `doctor`

```
  ✓  Python          3.14.6
  ✓  Playwright      1.62.0
  ✓  Browser         chromium 151.0.7922.34
  ✓  Configuration   valid (configs/local.toml)
  ✓  Proxy file      proxies.txt: 6854 proxies, 0 malformed, 0 blank, 0 comments
  ✓  Results dir     results is writable
  ✓  Target URL      http://127.0.0.1:8000/ -> 200 (direct, no proxy)
```

Checks run in dependency order, so the first failure is the root cause. The Target check is a
**warning, never a failure**, and says `(direct, no proxy)` — a target being down now does not
mean your lab is broken, and a pass there does not prove the proxied path works.

---

## Concurrency

Concurrency is bounded by an `asyncio.Semaphore` and structured with `asyncio.TaskGroup`.
`concurrency = 1` is sequential via a semaphore of one — there is no second code path to drift.

**Concurrency overlaps waiting, not working.** Measured on an M-series Mac, 12 sessions:

```
A  work-bound (dwell 0s)              B  wait-bound (dwell 1s)
conc  wall   speedup  eff   nav       conc  wall   speedup  eff   nav    peak RSS
  1   1.30s    1.00x  100%   9.7ms      1  13.39s    1.00x  100%   9.9ms    257 MB
  2   0.57s    2.29x  114%  10.8ms      2   7.48s    1.79x   89%  11.3ms    341 MB
  4   0.46s    2.79x   70%  15.8ms      4   3.61s    3.71x   93%  16.0ms    513 MB
  8   0.46s    2.82x   35%  24.0ms      8   2.46s    5.45x   68%  23.7ms    855 MB
 12   0.64s    2.04x   17%  67.4ms     12   1.45s    9.23x   77%  48.0ms   1147 MB
```

Three things to take from this:

1. **Workload B scales to 9.2×; workload A saturates at 2.8× and then *regresses*.** B's time is
   `asyncio.sleep` — pure waiting, which overlaps perfectly. A's time is Chromium creating
   contexts and rendering — real CPU work in another process, which does not.
2. **Navigation latency degrades in both**, 10 ms → 48–67 ms, against an unchanged loopback
   server. That increase is **the lab contending with itself.** Latency figures are only
   comparable at equal concurrency; never compare across levels and attribute it to the target.
3. **Memory is the binding constraint**: ~95 MB per live context at peak. The `MAX_CONCURRENCY`
   ceiling of 64 extrapolates to roughly 6 GB.

*(The 114% efficiency at A/2 is a measurement artifact — the concurrency-1 baseline paid for
cold-start warm-up that later runs inherited. Reported as measured rather than quietly dropped.)*

---

## Metrics and output

Each run writes two files:

```
results/<experiment_id>.jsonl          one JSON object per session
results/<experiment_id>.summary.json   aggregates + config + environment
```

```json
{"experiment_id":"20260911T124023-c0baa4","session_id":1,"status":"completed",
 "proxy_label":"1.2.3.4:8080","started_at":1789129335.27,"total_ms":30012.4,
 "setup_ms":34.7,"navigation_ms":8.6,"dwell_ms":30000.0,"http_status":200,
 "error_type":null,"error_message":null,"attempts":1}
```

JSONL because it is **appendable** (a 25-minute run must not hold everything in memory),
**streamable** (`tail -f` works mid-run; each row is flushed as it completes, so a crash at
session 48 of 50 leaves 47 valid rows), and **typed** — a missing timing stays `null` rather
than becoming CSV's `""`.

### Session statuses

| status | meaning | retryable |
| --- | --- | --- |
| `completed` | navigated, matched `expected_status`, dwelled | — |
| `failed_setup` | context or page could not be created | **no** |
| `failed_proxy` | `ERR_PROXY_*` / `ERR_TUNNEL_*` / `ERR_SOCKS_*` | yes |
| `failed_navigation` | DNS, refused, reset, TLS | yes |
| `failed_timeout` | navigation exceeded `timeouts.navigation_ms` | yes |
| `failed_status` | loaded, but not `expected_status` | 5xx only |

### Proxy failures are reported as a bracket, not a number

```
Proxy attribution
  unreachable (certain):   0     the proxy could not be reached at all
  any failure via proxy:   2     every failure on a proxied session
  2 failure(s) cannot be attributed to proxy or target
```

An overloaded proxy usually *answers* with 502 or 504 rather than refusing the connection, so
the browser sees an ordinary HTTP status and the session is recorded as `failed_status`. A metric
counting only `failed_proxy` would report **zero** while the proxy caused every failure — which
is exactly what happened in experiment D1 before this was fixed.

The tempting correction, "a 5xx on a proxied session is a proxy failure", is the opposite
mistake: it blames the proxy for the target's own outages. The `Via` header doesn't settle it
either, since a proxy adds it to responses it forwards and responses it generates alike.

So the truth is reported as a range. When the two numbers agree, attribution is certain; when
they differ, the gap is precisely what cannot be known from the client side.

`failed_setup` is **not** retryable on purpose: it means the browser is unhealthy, and retrying
broken infrastructure turns a loud two-second failure into a confusing twenty-minute one.
**Retry the request, never the infrastructure.**

### Timing fields record phases that *completed*

A phase that failed reports `null`, not the time it spent failing. A column meaning "time to
succeed" in some rows and "time to fail" in others cannot be averaged — and proxy failures are
often *fast*, so mixing them would make a run that got worse look like it got faster. Averages
are taken over completed sessions only. `total_ms` still covers a failure's duration.

### Summary

```
Experiment 20260911T124023-c0baa4
--------------------------------------------
Sessions:                                4
Completed:                               4
Failed:                                  0

Average setup:                    133.3 ms
Average navigation:                18.1 ms
Average dwell:                    301.7 ms
Average duration:                 471.2 ms

Success rate:                      100.0%
Sessions via proxy:                      0
Wall clock:                         1.0 s
```

### Analysing without re-running

```bash
cat results/*.jsonl | jq -r 'select(.status != "completed") | .error_message' | sort | uniq -c
cat results/*.jsonl | jq -s 'group_by(.proxy_label) | map({proxy: .[0].proxy_label, n: length})'
```

---

## Testing

```bash
uv run pytest -q -m "not integration"   # 264 tests, ~3 s   — the inner loop
uv run pytest -q                        # 306 tests, ~38 s  — everything
uv run pytest -q -m integration         #  42 tests         — real browser
uv run ruff check . && uv run ruff format --check .
uv run mypy app lab tests               # strict mode
```

The split is load-bearing. **A test suite you stop running because it is slow protects
nothing.** 245 tests need no browser, no network and almost no disk, because every layer takes
its collaborators as parameters:

```python
metrics = await run_experiment(run_session=FakeSession(), count=10)
```

Notable tests, as a guide to what the design is protecting:

- `test_the_runner_does_not_pull_in_playwright` — the architecture rule, in a subprocess.
- `test_the_concurrency_limit_is_never_exceeded` — a fake that records peak concurrent sessions.
- `test_navigation_timing_tracks_a_delay_we_caused` — instrument calibration. The bench sleeps
  800 ms; if `navigation_ms` came back at 40 ms the timer would be measuring the wrong thing.
- `test_password_never_appears_in_any_rendering` — parametrized over eight output paths.
- `test_context_is_closed_even_when_the_body_raises` — the original project's exact defect.
- `test_the_real_proxy_file_contains_no_credentials` — a tripwire; if `proxies.txt` ever gains
  credentials it becomes a secret and must leave git.

---

## Troubleshooting

**`Executable doesn't exist at .../chrome-headless-shell`**
`playwright install chromium` was not run. `uv sync` installs the client, not the browser.

**`net::ERR_UNSAFE_PORT`**
Chromium refuses to *navigate* to ~80 ports (1, 7, 22, 25, 6000, …) regardless of what is
listening. Use a high port for your target. The restriction applies to navigation targets
only — the same port works fine as a proxy address.

**Proxy seems ignored**
It is not, on Chromium via Playwright — verified by test. Note that raw Chrome's
`--proxy-server` *does* bypass loopback by default; Playwright does not. To check yourself,
point a context at a closed port and confirm navigation fails with
`ERR_PROXY_CONNECTION_FAILED`.

**`ExceptionGroup` traceback**
`asyncio.TaskGroup` wraps child exceptions. The CLI unwraps groups whose leaves are all
`LabError` and prints them cleanly; anything else is a genuine bug and keeps its traceback on
purpose.

**`FileExistsError` writing results**
Result files are opened exclusively and never overwritten — they record something that
happened. Use a different `experiment_id` or clear `results/`.

**Weird `invalid hostname '﻿1.2.3.4'` on line 1 only**
A byte-order mark. `load_proxies` reads `utf-8-sig`, so this should not happen; if it does, the
file is not UTF-8.

**Line endings**
`.gitattributes` sets `* text=auto`, so `proxies.txt` is stored LF in the repository while a
Windows or legacy working copy may be CRLF. The parser handles both.

**High latency at high concurrency**
Expected — see [Concurrency](#concurrency). You are measuring your own machine.

---

## Security

**Credentials are protected by construction, not by discipline.**

- `Proxy` defines its own `__repr__` printing an allowlist, so a password cannot reach a log
  line, traceback, or pytest assertion diff through an object. This is *fail-safe*: a field
  added later is excluded by default. `field(repr=False)` would be fail-open — it protects only
  the field someone remembered to flag.
- `SessionResult` holds `proxy_label: str`, a pre-redacted string, **never a `Proxy`**. Results
  are written to disk, so this makes a credential in a results file structurally impossible.
- `Proxy.server` omits credentials; Playwright takes `username`/`password` as separate keys.
- Parse errors redact before storing raw input — the error path is a leak vector too.
- The log formatter scrubs `user:pass@host` and `password=…` patterns, including inside
  exception tracebacks. This is **defence in depth, not the defence**: it catches accidents of
  *format*, never intent. `logger.info("pw is %s", proxy.password)` would still leak.

**Where secrets belong.** Committed config files are for settings, not secrets.
`configs/local*.toml`, `.env`, `proxies.local.txt` and `results/` are gitignored. If you move to
proxies with credentials, put them in an ignored file — do not edit `proxies.txt` in place, and
note that `proxies.txt` then becomes a secret and must leave git history.

The included `proxies.txt` (6,854 entries) contains **no credentials**, and a test asserts that.

**Chromium's sandbox is left on.** The original passed `--no-sandbox`; a test asserts it stays
absent. If you containerise, solve it with a seccomp profile rather than disabling a real
security control.

---

## Architecture decisions

| Decision | Why | Rejected alternative |
| --- | --- | --- |
| **Playwright over Selenium** | Proxy is a per-*context* setting. With Selenium it is a browser launch argument, so N proxies means N browser processes. Contexts are 65× cheaper than processes. | Selenium: still right for real Safari, an existing Grid, or a house standard |
| **Async from day one** | `sync_api` and `async_api` are not interchangeable; choosing sync would have meant a mechanical rewrite of every call site at the concurrency phase | Sequential-then-rewrite |
| **`TaskGroup` over `gather`** | `gather` propagates the first exception but leaves siblings running detached — orphaned browser contexts after the run has already failed | `gather`, at the cost of leaks |
| **Results, not exceptions, at the session boundary** | A failed session is an expected *outcome*; one that raised would produce no row, so the failure rate would be computed only from sessions that worked | Raising, and losing the data point |
| **Timings record completed phases only** | A column mixing "time to succeed" and "time to fail" cannot be averaged; failures are often fast, so mixing them makes degradation look like speedup | Recording time-to-failure in the same field |
| **Consecutive failures, not failure rate, for abort** | A rate needs a second knob for minimum sample size and cannot trip until ~half the run is gone. Clustering is what a real outage looks like | `abort if >50% fail` |
| **Unknown config keys are errors** | A silently ignored typo runs cleanly and measures the wrong thing | Warn and continue |
| **Hand-rolled validation, not pydantic** | ~150 lines of stdlib vs a dependency with a compiled core; full control of messages and unknown-key policy | pydantic — the right call for a production service or a much larger config surface |
| **argparse, not click** | Three commands do not justify a second runtime dependency | click — worth it past ~6 commands or for shell completion |
| **Retries off by default** | They turn "success rate" into "success rate given up to N attempts". That should be an explicit choice | Retrying by default and quietly improving everyone's numbers |
| **No jitter on backoff** | Jitter trades reproducibility for herd protection. Add it before pointing this at anything shared | Jittered backoff |
| **Proxies assigned before tasks start** | Keeps rotation deterministic even though execution order is not | Pulling from the pool inside each task |

---

## Limitations

Stated plainly, because a lab that overstates its own accuracy is worse than no lab.

**Measurement**

- **Latency is not comparable across concurrency levels.** At concurrency 12, navigation latency
  is ~5× the concurrency-1 figure against an unchanged server. That is contention in the lab.
- **Third-party targets are poor instruments.** Their CDN, caching, rate limits and A/B
  behaviour drift independently of your experiment. Use a target you control.
- **Public proxy lists are not a measurement substrate.** Free-list proxies run 90–98% dead and
  the dead fraction moves hour to hour, so proxy decay confounds whatever you are varying. The
  bundled `proxies.txt` is excellent *parser* test data and a realistic large pool; it should
  not be what you measure on. Run your own proxies for experiments.
- **CPU figures are unreliable** for sub-second runs; the sampler polls `ps` every 250 ms. RSS is
  trustworthy; CPU is not.

**Implementation**

- **Proxy failures can only be bracketed, not counted.** When a proxy answers 502 rather than
  refusing a connection, no client-side signal distinguishes it from the target answering 502.
  Reported as a range; see [Metrics and output](#metrics-and-output).
- **Error classification is string matching on Chromium's error text.** A browser update
  renaming a code would silently reclassify failures. It degrades to `failed_navigation` rather
  than crashing, and is covered by tests.
- **No proxy health tracking or quarantine.** A proxy that has failed 20 times keeps getting
  handed out. `FailureTracker` is the seam where it belongs; it is unbuilt because with a small
  controlled proxy set there is no use case, and building it against an imagined one means
  guessing at quarantine duration and recovery.
- **A cleanup failure misclassifies.** If `context.close()` raises *after* a successful dwell,
  the session is recorded as `failed_navigation`. Rare, and deliberately left as-is.
- **Memory scales with `count`**: every task is created up front. Fine at thousands of sessions;
  past that the right shape is a fixed worker pool over an `asyncio.Queue`.
- **SOCKS5 with authentication is unverified.** Test it before trusting it.
- **The bench proxy is not production software.** One request per connection, no chunked
  request bodies, no caching. Fine for measuring a client; use tinyproxy or Squid otherwise.

**Scope**

- No detection evasion, no CAPTCHA handling, no fingerprint spoofing — by design, permanently.

---

## Licence

MIT — see [LICENSE](LICENSE).

Note: the project this replaces is Apache-2.0. This is a from-scratch rewrite that shares no
code with it; the only file carried over is `proxies.txt`, which is data rather than a
copyrightable work. If you intend to distribute this, confirm that licence choice yourself.
