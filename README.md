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

Then either copy `.env.example` to `.env` and put your key in it, or pass the key through your MCP client's config (below), which is the tidier option.

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

### Getting an API key

Free, and no payment details required. Only the two statistics tools need it.

1. Sign in at [console.cloud.google.com](https://console.cloud.google.com/) with a Google account. Accept the terms if you're asked to; ignore any prompt about a free trial or billing, since none of this needs it.
2. Create a project from the project dropdown in the top bar, next to the Google Cloud logo. Click **New project**, give it a name like `rozetta`, leave the location as-is, and hit **Create**. It takes a few seconds.
3. Make sure that project is the one selected in the dropdown. This is the step people miss, and it leads to enabling the API on one project while the key belongs to another.
4. Go to [the YouTube Data API v3 page](https://console.cloud.google.com/apis/library/youtube.googleapis.com) and click **Enable**.
5. Go to **APIs & Services → Credentials**, click **Create credentials**, and pick **API key**. It appears immediately. Copy it.
6. Optional but worth doing: click **Edit API key**, and under **API restrictions** choose **Restrict key** and select YouTube Data API v3. That way a leaked key can't be used against anything else. Leave application restrictions set to None, since this runs as a local process with no fixed IP or referrer.

The key looks like `AIza...` and is about 39 characters. Paste it into the Claude Desktop config above, or into `.env`.

Your quota is 10,000 units a day, which is roughly 10,000 video or channel lookups. That's plenty for interactive use.

## Tests

```bash
.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
```

155 tests, 98% coverage. They cover every documented failure mode, not just the happy paths: missing captions, private and age-restricted videos, the extraction library breaking, unavailable translations, hidden like counts, all four channel URL formats including the legacy fallback failing, quota exhaustion blocking before a request goes out, and the API key never appearing in a tool schema.

Nothing in the suite touches the network. The Data API is stubbed through an httpx mock transport and the transcript library is stubbed out.

## Not in v1

No video download, no search tool, no OAuth or private video access, no built-in summarisation, no caching.
