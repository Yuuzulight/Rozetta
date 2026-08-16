# Rozetta

An MCP server that gives a model access to YouTube transcripts and public statistics.

Rozetta doesn't summarise anything. It fetches clean data and hands it over; whatever calls it does the reading and the thinking. That split is deliberate, and it's why the server works the same whether it's Claude Desktop on the other end or something else entirely.

It speaks stdio only, so the client spawns it as a subprocess per session. There's no hosted mode and nothing listening on a port.

## Why transcripts and statistics are separate things

They come from completely different places, and pretending otherwise would hide a real difference in reliability.

Transcripts come from [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api), which reads the same caption tracks the web player uses. It's unofficial and needs no key. There is no official alternative: Google's Captions API requires OAuth as the video's own owner, which is useless for looking at anyone else's public video. So this is the only game in town, and it can break if YouTube reorganises things.

Statistics come from the official YouTube Data API v3. That needs a free API key, gives you 10,000 quota units a day, and is stable.

Search is not included in v1, on purpose. `search.list` costs 100 units per call against a 10,000 unit budget, which makes it far and away the easiest way to burn a whole day's quota without noticing.

## Tools

### `get_video_info(url_or_id)`

Title, channel, duration, and the first two or three sentences of the description. One quota unit.

This is the "did you mean this video?" call. The tool description tells the model to call it first and show the result to the user before pulling a transcript. That's a convention, not a rule the server enforces — there's no session state tracking who called what, and `watch_video` works fine on its own.

It doesn't estimate transcript length. Working that out properly means fetching the transcript, which costs the same as just calling `watch_video`, and guessing from duration is bad enough to mislead: a 40 minute lecture and a 40 minute music video have wildly different amounts of speech in them.

### `watch_video(url_or_id, target_language=None)`

The full transcript, no length cap. Costs no quota.

You get `transcript_text` as plain prose with timestamps stripped, plus `transcript_segments` with the raw `{text, start, duration}` cues if you need to cite a moment.

Nothing is truncated. Any cap here would be an arbitrary guess at the caller's context budget, and the two-step flow with `get_video_info` already gives the model a chance to warn someone before it pulls an hour of speech.

When several caption tracks exist, Rozetta picks the one in the language actually spoken. That sounds obvious but isn't: a well-subtitled video can carry a dozen human-written tracks and YouTube doesn't list them original-first, so naively taking the first human-written track can hand you Arabic subtitles for an English video. The auto-generated track is the tell, since YouTube only ever generates captions by transcribing the audio.

Pass `target_language` (`"es"`, `"ja"`, and so on) to get another language. If the video has a real caption track in that language it's used directly. Otherwise YouTube's own translation is applied, through the library's `.translate()` — no second dependency, no LLM translation step. If the language isn't available at all the call fails and lists the ones that are. It will never quietly hand back the original in a different language than you asked for.

Worth knowing before you rely on it: **YouTube throttles translated captions far harder than ordinary ones.** Measured directly — a plain caption fetch succeeded, a `.translate()` call from the same address seconds later was refused, and a plain fetch straight after succeeded again. That's YouTube's gate, not this server's, and it happens with the transcript library called directly too.

So asking for a language the video has a real track in is reliable. Asking for one it doesn't fails a fair amount of the time. The error says so and lists the languages that do have real tracks, rather than telling you to retry into the same wall.

Failures are kept distinguishable, because they call for different reactions:

| What happened | What you get |
|---|---|
| Video has no captions | "no transcript available" — a fact about the video |
| Private or age-restricted | explicit error; Rozetta does not authenticate, public videos only |
| The extraction library itself broke | "transcript extraction failed — may indicate an upstream YouTube change" |

That last one is worth retrying later. The first one isn't.

There's no fallback extraction path, and that's a decision rather than an omission. Scraping the watch page by hand is the same category of unofficial, breaks for much the same reasons, and would just be more code with a similar failure rate.

One thing to know in practice: YouTube rate-limits by IP. Fire off several transcript requests back to back and you'll start getting the extraction-failure error until it cools off. That's the error working correctly, not a bug.

### `get_video_stats(url_or_id)`

Views, likes, comments, publish date, duration, tags. One quota unit.

`like_count` and `comment_count` come back as `null` when the uploader has hidden likes or turned off comments. Null means "not published", not zero, and conflating the two would be a quiet lie.

This overlaps with `get_video_info` on title and channel, and stays separate anyway. The two answer different questions, and merging them would mean every "is this the right video?" check also drags in vote and comment data nobody asked for.

Internally the wrapper batches up to 50 IDs into a single `videos.list` call. It's one quota unit either way, so the saving is round-trips rather than budget.

### `get_channel_stats(channel_url_or_id)`

Subscribers, video count, total views, creation date, and a rough recent upload cadence. Two quota units: one for the channel, one for the cadence.

`recent_upload_cadence` comes from a `playlistItems.list` call against the channel's uploads playlist, which costs 1 unit. Deriving it from `search.list` would cost 100 for the same answer. It reads the most recent page of 50 uploads, so for a very busy channel it reports a lower bound ("at least ~11.7 videos/week") rather than understating.

`subscriber_count` is `null` for channels that hide it.

#### The four channel URL formats are not equally reliable

This is the part worth reading before you trust the output.

| Format | How it's resolved | Cost | Reliability |
|---|---|---|---|
| `@handle` | `channels.list?forHandle=` | 1 unit | Official, dependable |
| `UC...` channel ID | `channels.list?id=` | 1 unit | Official, dependable |
| `/user/Username` | `channels.list?forUsername=` | 1 unit | Official, but only works for channels that still carry an old-style username |
| `/c/CustomName` | fetch the page, dig the ID out of the HTML | 1 unit + a page fetch | Unofficial, can break |

The last row isn't laziness. `/c/` links are a vanity redirect layer YouTube built on top of channel IDs for search engines, and the API never indexed them — there is no parameter to query. Reading the page is the only way, which puts it in the same bracket as the transcript library.

Legacy `/user/` URLs get the same treatment as a second attempt if `forUsername` comes back empty, which happens a lot now that old usernames have been retired.

When the page fallback fails you get a clear error asking for the `@handle` or the channel ID, rather than something vague.

## API key handling

`YOUTUBE_API_KEY` is read once, from the environment, when the server starts. It is never a tool argument and never appears in any tool's input schema. That's on purpose: MCP clients log tool calls, and a key passed as an argument ends up in those logs. There's a test asserting the key can't appear in a schema, so a future change can't quietly reintroduce it.

It also travels to Google as an `X-goog-api-key` header rather than a `key=` query parameter. The query-string form is what the docs show, and it's a trap: httpx logs every request URL at INFO, an MCP server's stderr gets captured into the client's log files, and the result is your key sitting in plaintext on disk after any statistics call. Learned that one the hard way. A second test asserts no request URL ever contains the key, and httpx is pinned to WARNING at startup as a backstop.

Transcripts don't need a key. If you never set one, `watch_video` still works and the two statistics tools fail with a clear message.

## Quota

The tracker keeps a running count in `~/.rozetta/quota.json` (override with `ROZETTA_QUOTA_FILE`). Since the client spawns a new process per session, the count has to live outside the process.

The day rolls over at **midnight Pacific**, which is what Google actually uses. Not your local midnight, and not UTC. This is a well-known way to get burned: your counter resets, Google's doesn't, and you spend a while confused about the 403s.

Before every Data API call Rozetta checks whether the request would fit in what's left. If it wouldn't, you get "quota exhausted for today, resets at ..." with the actual time in both Pacific and local, instead of a bare 403 from Google.

Costs, from Google's own table:

| Endpoint | Units | Used by |
|---|---|---|
| `videos.list` | 1 | `get_video_info`, `get_video_stats` |
| `channels.list` | 1 | `get_channel_stats` |
| `playlistItems.list` | 1 | `get_channel_stats` (cadence) |
| `search.list` | 100 | nothing — listed so the cost stays visible |

There's no caching in v1. Repeat calls within a session cost 1 unit each against a 10,000 unit budget, which isn't a real problem yet, and a cache would bring invalidation and staleness questions that aren't worth answering at this scale. It's the obvious first addition if usage ever grows.

## Setup

Requires Python 3.11 or newer.

```bash
git clone https://github.com/Yuuzulight/Rozetta.git
cd Rozetta
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

On macOS or Linux that last line is `.venv/bin/python -m pip install -e ".[dev]"`.

### Getting a YouTube API key

Free, and no payment details required. Skip this if you only want transcripts — those work without a key, and only `get_video_stats` and `get_channel_stats` need one.

If you've never used Google Cloud, the whole thing takes about five minutes.

1. **Sign in** at [console.cloud.google.com](https://console.cloud.google.com/) with any Google account. Accept the terms if prompted. If it offers a free trial or asks for a card, ignore it — nothing here needs billing enabled.

2. **Create a project.** Use the project dropdown in the top bar, immediately right of the "Google Cloud" logo — it may read "Select a project". Click it, then **New project**. Name it something like `rozetta`, leave Location as "No organization", and click **Create**. It takes a few seconds to appear.

3. **Select that project** in the same dropdown. Don't skip this. It's the most common way this goes wrong: you enable the API on whichever project was already selected, create the key under another, and then spend a while wondering why a valid-looking key returns 403.

4. **Enable the API.** Open [the YouTube Data API v3 page](https://console.cloud.google.com/apis/library/youtube.googleapis.com) and click **Enable**. Check the project name shown on that page is the one you just made.

5. **Create the key.** Go to **APIs & Services → Credentials**, click **+ Create credentials**, and choose **API key**. It appears in a dialog straight away. Copy it.

6. **Restrict it.** In that same dialog click **Edit API key**, scroll to **API restrictions**, choose **Restrict key**, tick **YouTube Data API v3**, and save. Thirty seconds of work that means a leaked key can't be spent on anything else in your project. Leave *Application* restrictions on "None" — this runs as a local process with no stable IP or referrer.

Keys start with `AIza` and are 39 characters. Put yours in `.env` (copy `.env.example`), or pass it through your MCP client's config — whichever suits the client you're using.

Treat it as a secret. It isn't a password, but anyone holding it can spend your daily quota. If it ever ends up somewhere public, delete it in the Credentials screen and make a new one; it costs nothing.

#### Check it works

```bash
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'src'); import server, stats; server.load_dotenv(); import os; k=os.environ.get('YOUTUBE_API_KEY'); print('key loaded:', bool(k)); stats.configure(k); print(stats.get_video_stats('dQw4w9WgXcQ').view_count, 'views')"
```

A view count means the key, the project, and the API enablement are all correct. "YOUTUBE_API_KEY is not set" means it never reached the server. A message about the key being rejected means the key or the enablement is wrong — usually step 3.

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` on Windows, or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS. Create it if it isn't there.

```json
{
  "mcpServers": {
    "rozetta": {
      "command": "C:\\GitHub Projects\\Rozetta\\.venv\\Scripts\\python.exe",
      "args": ["C:\\GitHub Projects\\Rozetta\\src\\server.py"],
      "env": {
        "YOUTUBE_API_KEY": "PASTE_YOUR_KEY_HERE"
      }
    }
  }
}
```

Use the venv's Python rather than a bare `python`, and absolute paths throughout. Claude Desktop doesn't inherit your shell's PATH or working directory. Backslashes need doubling because it's JSON.

Restart Claude Desktop completely afterwards — quit it from the system tray, not just close the window.

**Edit that file with Claude Desktop fully closed.** Recent versions rewrite it from memory when they exit, so an edit made while the app is running gets silently discarded — the server never appears, and nothing tells you why.

### Claude Desktop, as a plugin

Newer builds take a `.plugin` bundle instead, which sidesteps the config file entirely.

**The easy way:** download `rozetta.plugin` from the [latest release](https://github.com/Yuuzulight/Rozetta/releases/latest) and drag it into Claude Desktop's extensions settings screen. Nothing to clone or configure.

That bundle launches the server with [uv](https://docs.astral.sh/uv/), which is the only prerequisite — uv fetches the code and its dependencies itself, so there's no virtualenv to manage and no Python version to match. Set your key as a `YOUTUBE_API_KEY` environment variable and restart the client so it inherits it.

Worth doing once before installing, because the first launch pulls about 40 packages and can outlast a client's startup timeout:

```bash
uvx --from git+https://github.com/Yuuzulight/Rozetta rozetta
```

It will sit there waiting for input, which is what a healthy stdio server does. Ctrl+C out of it.

#### Building a bundle yourself

```bash
python packaging/build_plugin.py              # portable, tracks the default branch
python packaging/build_plugin.py --ref v0.1.0 # portable, pinned to a tag
python packaging/build_plugin.py --local      # points at this checkout's venv
```

Output lands in `dist/`. The `--local` flavour runs your working tree, so edits take effect without rebuilding, but it only works on the machine that built it — the script refuses to put a machine-specific path in a portable bundle.

A plugin is just a zip with `.claude-plugin/plugin.json` and a `.mcp.json` at the root. Zip the *contents*, not the parent folder; a bundle with everything nested one level down is rejected.

The local flavour reads your key from `.env` in the repo root, since the server looks there at startup. Write that file as plain UTF-8 — the loader handles a BOM, but PowerShell's `Set-Content -Encoding utf8` adds one and other tools are less forgiving.

## Tests

```bash
.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
```

161 tests, 98% coverage. They cover every documented failure mode, not just the happy paths: missing captions, private and age-restricted videos, the extraction library breaking, unavailable translations, hidden like counts, all four channel URL formats including the legacy fallback failing, quota exhaustion blocking before a request goes out, and the API key appearing in neither a tool schema nor a request URL.

Nothing in the suite touches the network. The Data API is stubbed through an httpx mock transport and the transcript library is stubbed out.

## Not in v1

No video download, no search tool, no OAuth or private video access, no built-in summarisation, no caching.

## License

MIT. See [LICENSE](LICENSE).
