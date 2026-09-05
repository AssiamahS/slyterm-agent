# slyterm NOTES

- GitHub Models free tier rejects requests over ~8K input tokens — full-size agent CLIs (opencode, gemini-cli) can't run on it; this agent trims context to ~5.5K tokens to fit.
- DuckDuckGo serves python urllib a bot-challenge page but answers curl with identical headers — http_get shells out to curl for that reason.
- Google OAuth codes (agy/Antigravity login) are PKCE-bound: a code only redeems inside the same CLI run that printed the URL, never later.
- Repo is github.com/AssiamahS/slyterm-agent — `slyTerm` on the same account is an unrelated Swift app; never push this project there.
- streaming SSE tool_calls arrive as fragments keyed by index — stitch name/arguments strings together; free-tier 429s raise BEFORE the stream opens, so model-hop logic is unchanged
cloud runner verified.

- SlyTermBar (macos/SlyTermBar.swift): menu bar app, left-click = session menu, right-click = Close. Build: swiftc -O into ~/Applications/SlyTermBar.app (ad-hoc signed, LSUIElement). NEXT VERSION: embed a ttyd web terminal in a popover like MenuToDo3 instead of spawning Terminal windows.

- SlyTermBar v0.3: ttyd live terminal SHIPPED — app spawns ttyd on 127.0.0.1:7699 running slyterm, popover = WKWebView to it (real REPL, streaming, Ctrl-C). Icon = GitHub avatar (slytermOS repo has no logo file). Needs NSAllowsLocalNetworking in Info.plist.
