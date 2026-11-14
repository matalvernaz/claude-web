"""Event sounds when the page catches up on a running chat, and when audio is suspended.

Matt, 2026-11-14: "if you switch to a session that's in progress, all the
sounds for the messages you missed play at once". On his iPhone Safari freezes
the page while he is away; coming back, the page reconnects and replays every
event it missed through the same code that plays the sounds. A suspended audio
context did the same thing another way: its clock stops, so every cue asked
for meanwhile started together on the first tap. And sounds "don't always
play" after reopening a chat, because the audio was only ever unlocked on the
first pointerdown or keydown of the page.

The page runs against a stand-in AudioContext that records every tone it is
asked to play, with the context's state at that moment.
"""
from __future__ import annotations

import json

from tests.test_local_ui import browser, playwright, ui  # noqa: F401  (shared browser harness)

# The first note of each cue says which cue it is (static/app.js scheduleCue).
FIRST_NOTE = {
    (660, "sine"): "done",
    (440, "sine"): "error",
    (740, "triangle"): "permission",
    (880, "sawtooth"): "attention",
    (600, "square"): "autofire",
    (1320, "sine"): "task_done",
    (300, "sine"): "task_error",
    (660, "triangle"): "timeout",
    (698, "triangle"): "tool",
}

# A stand-in for the browser's audio, streams the test feeds by hand, and a
# clock the test can move forward.
HARNESS = """(() => {
  window.__tones = [];
  window.__cueReads = 0;
  window.__resumeCalls = 0;
  window.__pendingResumes = [];
  class FakeAudioContext {
    constructor() {
      this.state = localStorage.getItem("test.audio-start") || "running";
      this.sampleRate = 48000;
      this.destination = {};
      window.__ctx = this;
    }
    // Each cue reads the clock once before scheduling its notes, which is
    // how the notes are grouped into cues.
    get currentTime() {
      window.__cueReads += 1;
      return 0;
    }
    resume() {
      window.__resumeCalls += 1;
      if (localStorage.getItem("test.resume") === "blocked") {
        return new Promise((resolve) => window.__pendingResumes.push(() => {
          this.state = "running";
          resolve();
        }));
      }
      this.state = "running";
      return Promise.resolve();
    }
    createGain() {
      return {
        gain: { value: 1, setValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect(node) { return node; },
      };
    }
    createOscillator() {
      const context = this;
      const oscillator = {
        type: "", frequency: { value: 0 },
        connect(node) { return node; },
        stop() {},
        start() {
          window.__tones.push({
            cue: window.__cueReads, freq: oscillator.frequency.value,
            type: oscillator.type, state: context.state,
          });
        },
      };
      return oscillator;
    }
    createBuffer() { return {}; }
    createBufferSource() { return { buffer: null, connect() {}, start() {} }; }
  }
  window.AudioContext = FakeAudioContext;
  window.webkitAudioContext = FakeAudioContext;
  window.__releaseResumes = () => {
    const pending = window.__pendingResumes.splice(0);
    pending.forEach((release) => release());
  };

  window.__timeShift = 0;
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + window.__timeShift;

  window.__streams = [];
  const realFetch = window.fetch;
  window.fetch = (url, options) => {
    const text = String(url);
    if (!text.includes("/api/chat/stream/")) return realFetch(url, options);
    let controller;
    const body = new ReadableStream({ start(c) { controller = c; } });
    window.__streams.push({ url: text, controller });
    if (options && options.signal) {
      options.signal.addEventListener("abort", () => {
        try { controller.error(new DOMException("Aborted", "AbortError")); } catch (_) {}
      });
    }
    return Promise.resolve(new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
  };
  window.__push = (index, events) => {
    const frames = events.map((event) => "data: " + JSON.stringify(event) + "\\n\\n").join("");
    window.__streams[index].controller.enqueue(new TextEncoder().encode(frames));
  };
  window.__end = (index) => window.__streams[index].controller.close();
})();"""


def _cues(page) -> list[str]:
    """The cues played so far, in order, by name."""
    names = []
    seen = set()
    for note in page.evaluate("window.__tones"):
        if note["cue"] in seen:
            continue
        seen.add(note["cue"])
        names.append(FIRST_NOTE.get((note["freq"], note["type"]), "unknown"))
    return names


def _tool(idx: int, name: str = "Bash") -> dict:
    return {"type": "assistant", "_idx": idx, "message": {"content": [
        {"type": "tool_use", "id": f"tool-{idx}", "name": name, "input": {"command": f"step {idx}"}},
    ]}}


def _result(idx: int) -> dict:
    return {"type": "result", "_idx": idx, "subtype": "success", "result": "Done."}


def _open_running_chat(ui, *, next_idx: int = 10):  # noqa: F811
    """A page opened on a chat whose turn is still going, joined at its tail."""
    page, state = ui
    state["session"] = {
        "provider": "claude", "messages": [{"role": "user", "text": "Fix the build"}],
        "live_run": {"run_id": "run-1", "active": True, "between_turns": False, "next_idx": next_idx},
    }
    state["active"] = {"active": True, "run_id": "run-1", "session_id": "saved",
                       "between_turns": False, "next_idx": next_idx}
    page.route("**/api/chat/active*", lambda route: route.fulfill(json=state["active"]))
    page.add_init_script(HARNESS)
    page.goto("http://local-ui.test/?session=saved")
    page.wait_for_function("window.__streams.length === 1")
    assert page.evaluate("window.__streams[0].url").endswith(f"start_index={next_idx}")
    return page, state


def _tool_lines(page) -> int:
    return page.locator("#transcript .tool-line").count()


def test_coming_back_after_a_long_absence_replays_what_was_missed_silently(ui):  # noqa: F811
    page, state = _open_running_chat(ui)
    page.evaluate("window.__push(0, [EVENT])".replace("EVENT", json.dumps(_tool(10))))
    playwright.expect(page.locator("#transcript .tool-line")).to_have_count(1)
    assert _cues(page) == ["tool"]

    # The phone was locked for a minute: no bytes arrived, and coming back
    # the page reconnects and asks for everything after event 10.
    state["active"]["next_idx"] = 20
    page.evaluate("window.__timeShift = 60000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("window.__streams.length === 2", timeout=5000)
    assert page.evaluate("window.__streams[1].url").endswith("start_index=11")

    missed = [_tool(11), _tool(12, "Read"), _result(13),
              {"type": "auto_fire", "_idx": 14, "events": []},
              _tool(15), _result(16)]
    page.evaluate("window.__push(1, EVENTS)".replace("EVENTS", json.dumps(missed)))
    playwright.expect(page.locator("#transcript .tool-line")).to_have_count(4)
    page.wait_for_timeout(300)
    assert _cues(page) == ["tool"], "the missed events played their sounds"

    # What happens from here on is news again.
    page.evaluate("window.__push(1, [EVENT])".replace("EVENT", json.dumps(_result(20))))
    page.wait_for_function("window.__tones.length > 1", timeout=5000)
    assert _cues(page) == ["tool", "done"]


def test_a_still_open_approval_in_the_backlog_still_calls_for_attention(ui):  # noqa: F811
    page, state = _open_running_chat(ui)
    state["active"]["next_idx"] = 13
    page.evaluate("window.__timeShift = 60000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("window.__streams.length === 2", timeout=5000)
    missed = [_tool(10), _tool(11),
              {"type": "permission_request", "_idx": 12, "id": "req-1", "tool": "Bash",
               "input": {"command": "rm -rf build"}}]
    page.evaluate("window.__push(1, EVENTS)".replace("EVENTS", json.dumps(missed)))
    page.wait_for_function("window.__tones.length > 0", timeout=5000)
    page.wait_for_timeout(300)
    assert _cues(page) == ["permission"]


def test_a_short_blip_still_sounds_the_end_of_the_turn(ui):  # noqa: F811
    """A connection that drops for a moment mid-turn must not swallow the
    end-of-turn chime just because it came over the reconnect."""
    page, state = _open_running_chat(ui)
    page.evaluate("window.__push(0, [EVENT])".replace("EVENT", json.dumps(_tool(10))))
    playwright.expect(page.locator("#transcript .tool-line")).to_have_count(1)
    state["active"]["next_idx"] = 13
    page.evaluate("window.__end(0)")
    page.wait_for_function("window.__streams.length === 2", timeout=5000)
    assert page.evaluate("window.__streams[1].url").endswith("start_index=11")
    page.wait_for_timeout(300)
    page.evaluate("window.__push(1, EVENTS)".replace("EVENTS", json.dumps([_tool(11), _result(12)])))
    page.wait_for_function("window.__tones.length > 1", timeout=5000)
    page.wait_for_timeout(300)
    cues = _cues(page)
    assert cues[-1] == "done"
    assert len(cues) <= 3


def _send_with_suspended_audio(ui):  # noqa: F811
    """A turn whose events all arrive while the browser has the sound
    suspended and is not letting it start yet."""
    page, state = ui
    state["chat_sse"] = "".join(f"data: {json.dumps(event)}\n\n" for event in [
        {"type": "run_started", "run_id": "run-2", "_idx": 0},
        _tool(1), _tool(2, "Read"), _result(3),
        {"type": "auto_fire", "_idx": 4, "events": []},
        _tool(5), _result(6),
    ])
    page.add_init_script("""
      localStorage.setItem('claude-web.provider', 'claude');
      localStorage.setItem('test.audio-start', 'suspended');
      localStorage.setItem('test.resume', 'blocked');
    """)
    page.add_init_script(HARNESS)
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("Go")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    playwright.expect(page.locator("#transcript .tool-line")).to_have_count(3)
    return page


def test_cues_asked_for_while_audio_is_suspended_do_not_pile_up(ui):  # noqa: F811
    page = _send_with_suspended_audio(ui)
    page.wait_for_timeout(1200)
    page.evaluate("window.__releaseResumes()")
    page.wait_for_timeout(200)
    tones = page.evaluate("window.__tones")
    assert [t for t in tones if t["state"] != "running"] == [], "notes were queued on a stopped clock"
    # Sound came back more than a second later: that turn is old news.
    assert _cues(page) == []


def test_sound_coming_back_at_once_plays_only_the_most_urgent_cue(ui):  # noqa: F811
    page = _send_with_suspended_audio(ui)
    page.evaluate("window.__releaseResumes()")
    page.wait_for_function("window.__tones.length > 0", timeout=5000)
    page.wait_for_timeout(200)
    assert _cues(page) == ["done"]


def test_any_click_restarts_sound_the_browser_suspended_again(ui):  # noqa: F811
    """A screen reader's activation can arrive as a bare click, and an
    iPhone suspends the sound again whenever Safari goes to the background."""
    page, _state = ui
    page.add_init_script("localStorage.setItem('test.audio-start', 'suspended')")
    page.add_init_script(HARNESS)
    page.goto("http://local-ui.test/")
    page.evaluate("document.body.click()")
    page.wait_for_function("window.__ctx && window.__ctx.state === 'running'", timeout=5000)
    first = page.evaluate("window.__resumeCalls")
    assert first >= 1

    page.evaluate("window.__ctx.state = 'suspended'")
    page.evaluate("document.body.click()")
    page.wait_for_function("window.__ctx.state === 'running'", timeout=5000)
    assert page.evaluate("window.__resumeCalls") > first
