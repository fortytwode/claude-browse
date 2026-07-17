// claude-browse local web viewer. Vanilla JS, no build step, no CDN --
// this has to work fully offline, same as the rest of the tool.
(function () {
  "use strict";

  var searchInput = document.getElementById("search-input");
  var hereToggle = document.getElementById("here-toggle");
  var sessionList = document.getElementById("session-list");
  var viewerTitle = document.getElementById("viewer-title");
  var viewerMeta = document.getElementById("viewer-meta");
  var threadSearch = document.getElementById("thread-search");
  var threadSearchCount = document.getElementById("thread-search-count");
  var transcript = document.getElementById("transcript");

  var activeSid = null;
  var currentTurns = null; // [{role, text}, ...] for the loaded session
  var sessionsSeq = 0; // stale-response guard for /api/sessions fetches

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function inlineFormat(escaped) {
    escaped = escaped.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    escaped = escaped.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    return escaped;
  }

  // Lightweight prose renderer: fenced ```code``` blocks -> <pre><code>,
  // remaining text -> paragraphs with **bold** / `inline code`. Not a
  // markdown engine -- these are prose turns, not full markdown documents.
  // Thread search only hides non-matching turns (see renderTranscript) and
  // relies on the browser's own find-in-page for highlight + navigation
  // within what's left visible -- simpler than hand-rolled highlight markup
  // and doesn't risk corrupting fence/bold detection when a match happens
  // to land right next to a fence or bold marker.
  function renderTurnBody(text) {
    var codeRe = /```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g;
    var parts = [];
    var lastIndex = 0;
    var match;
    while ((match = codeRe.exec(text)) !== null) {
      if (match.index > lastIndex) {
        parts.push({ type: "prose", text: text.slice(lastIndex, match.index) });
      }
      parts.push({ type: "code", text: match[2] });
      lastIndex = codeRe.lastIndex;
    }
    if (lastIndex < text.length) {
      parts.push({ type: "prose", text: text.slice(lastIndex) });
    }
    return parts
      .map(function (part) {
        if (part.type === "code") {
          return "<pre><code>" + escapeHtml(part.text) + "</code></pre>";
        }
        return part.text
          .split(/\n{2,}/)
          .filter(function (p) {
            return p.trim().length > 0;
          })
          .map(function (p) {
            return "<p>" + inlineFormat(escapeHtml(p)) + "</p>";
          })
          .join("");
      })
      .join("");
  }

  function debounce(fn, wait) {
    var timer = null; // per-debouncer timer: the two search boxes must not cancel each other
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, wait);
    };
  }

  function fetchSessions() {
    var q = searchInput.value.trim();
    var params = new URLSearchParams();
    if (q) params.set("q", q);
    if (hereToggle.checked) params.set("here", "1");
    var seq = ++sessionsSeq;
    fetch("/api/sessions?" + params.toString())
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (seq !== sessionsSeq) return; // a newer request superseded this one
        if (data.error) {
          // e.g. 503 while the CLI rebuilds the index — say so instead of
          // masquerading as an empty library, and retry once shortly.
          sessionList.innerHTML =
            '<div class="session-row-empty">' +
            "Search index is busy (" + data.error + "). Retrying…</div>";
          setTimeout(function () {
            if (seq === sessionsSeq) fetchSessions();
          }, 1500);
          return;
        }
        renderSessionList(data.sessions || []);
      })
      .catch(function () {
        if (seq !== sessionsSeq) return;
        sessionList.innerHTML =
          '<div class="session-row-empty">Could not reach the local server — ' +
          "is claude-browse --web still running in your terminal?</div>";
      });
  }

  function renderSessionList(sessions) {
    if (!sessions.length) {
      sessionList.innerHTML = '<div class="session-row-empty">No sessions found.</div>';
      return;
    }
    sessionList.innerHTML = "";
    sessions.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "session-row" + (s.session_id === activeSid ? " active" : "");
      row.dataset.sid = s.session_id;

      var top = document.createElement("div");
      top.className = "session-row-top";
      var folder = document.createElement("span");
      folder.className = "session-row-folder";
      folder.textContent = s.folder || "?";
      var time = document.createElement("span");
      time.textContent = (s.when || "") + " · " + s.provider_name;
      top.appendChild(folder);
      top.appendChild(time);

      var title = document.createElement("div");
      title.className = "session-row-title";
      title.textContent = s.title;

      row.appendChild(top);
      row.appendChild(title);
      row.addEventListener("click", function () {
        selectSession(s.session_id);
      });
      sessionList.appendChild(row);
    });
  }

  function selectSession(sid) {
    activeSid = sid;
    Array.prototype.forEach.call(sessionList.querySelectorAll(".session-row"), function (row) {
      row.classList.toggle("active", row.dataset.sid === sid);
    });
    threadSearch.value = "";
    threadSearch.hidden = true;
    threadSearchCount.textContent = "";
    viewerTitle.textContent = "Loading…";
    viewerMeta.textContent = "";
    transcript.innerHTML = "";

    fetch("/api/session/" + encodeURIComponent(sid))
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (sid !== activeSid) return; // user already clicked another session
        if (data.error) {
          viewerTitle.textContent = "Could not load session";
          viewerMeta.textContent = data.error;
          return;
        }
        currentTurns = data.turns || [];
        viewerTitle.textContent = data.meta.title;
        viewerMeta.textContent =
          data.meta.folder + " · " + data.meta.cwd + " · " + data.meta.provider_name +
          " · " + data.meta.msg_count + " messages";
        threadSearch.hidden = false;
        renderTranscript("");
      })
      .catch(function () {
        if (sid !== activeSid) return;
        viewerTitle.textContent = "Could not load session";
        viewerMeta.textContent =
          "Could not reach the local server — is claude-browse --web still " +
          "running in your terminal? Restart it and reload this page.";
      });
  }

  function renderTranscript(query) {
    if (!currentTurns || !currentTurns.length) {
      transcript.innerHTML = '<div id="transcript-empty">No messages in this session.</div>';
      threadSearchCount.textContent = "";
      return;
    }
    var lowerQuery = query.trim().toLowerCase();
    var matchCount = 0;
    var firstMatchEl = null;
    transcript.innerHTML = "";
    currentTurns.forEach(function (turn) {
      var isMatch = !lowerQuery || turn.text.toLowerCase().indexOf(lowerQuery) !== -1;
      if (isMatch) matchCount++;

      var el = document.createElement("div");
      el.className = "turn role-" + (turn.role === "user" ? "user" : "assistant");
      if (!isMatch) el.classList.add("thread-search-hidden");

      var roleLabel = document.createElement("div");
      roleLabel.className = "turn-role";
      roleLabel.textContent = turn.role === "user" ? "User" : "Assistant";

      var body = document.createElement("div");
      body.className = "turn-body";
      body.innerHTML = renderTurnBody(turn.text);

      el.appendChild(roleLabel);
      el.appendChild(body);
      transcript.appendChild(el);

      if (isMatch && lowerQuery && !firstMatchEl) firstMatchEl = el;
    });

    if (lowerQuery) {
      threadSearchCount.textContent =
        matchCount + " matching turn" + (matchCount === 1 ? "" : "s") +
        " (use your browser's find to highlight and step through matches)";
      if (firstMatchEl) firstMatchEl.scrollIntoView({ block: "center" });
    } else {
      threadSearchCount.textContent = "";
      // Open at the END of the conversation: the latest exchange is what
      // you almost always came to read; scroll up for history.
      transcript.scrollTop = transcript.scrollHeight;
    }
  }

  searchInput.addEventListener("input", debounce(fetchSessions, 250));
  hereToggle.addEventListener("change", fetchSessions);
  threadSearch.addEventListener("input", debounce(function () {
    renderTranscript(threadSearch.value);
  }, 150));

  // `--web --here` forces every session list server-side to this folder;
  // reflect that in the toggle so the UI doesn't imply it's optional.
  fetch("/api/meta")
    .then(function (res) {
      return res.json();
    })
    .then(function (meta) {
      if (meta.here_only_forced) {
        hereToggle.checked = true;
        hereToggle.disabled = true;
      }
    })
    .catch(function () {})
    .then(fetchSessions);
})();
