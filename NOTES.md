# slyterm NOTES

- GitHub Models free tier rejects requests over ~8K input tokens — full-size agent CLIs (opencode, gemini-cli) can't run on it; this agent trims context to ~5.5K tokens to fit.
- DuckDuckGo serves python urllib a bot-challenge page but answers curl with identical headers — http_get shells out to curl for that reason.
- Google OAuth codes (agy/Antigravity login) are PKCE-bound: a code only redeems inside the same CLI run that printed the URL, never later.
- Repo is github.com/AssiamahS/slyterm-agent — `slyTerm` on the same account is an unrelated Swift app; never push this project there.
- streaming SSE tool_calls arrive as fragments keyed by index — stitch name/arguments strings together; free-tier 429s raise BEFORE the stream opens, so model-hop logic is unchanged
cloud runner verified.
