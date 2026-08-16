# Rozetta

Four YouTube tools: `get_video_info`, `watch_video`, `get_video_stats`, and `get_channel_stats`.

Transcripts work with no API key at all. The two statistics tools need a free YouTube Data API v3 key.

## Requirements

[uv](https://docs.astral.sh/uv/) must be installed. Nothing else — no clone, no virtualenv, no matching Python version. uv fetches the server and its dependencies on first launch.

The very first launch installs around 40 packages. That can take long enough to trip a client's startup timeout, so it's worth running this once by hand first to warm the cache:

```
uvx --from git+https://github.com/Yuuzulight/Rozetta rozetta
```

It will sit waiting for input, which means it started correctly. Press Ctrl+C.

## API key

Set `YOUTUBE_API_KEY` as a user environment variable, then restart your MCP client so it inherits the value.

On Windows: search "environment variables" in the Start menu, open **Edit environment variables for your account**, and add it there.

Without a key, `watch_video` still works and the two statistics tools return a message saying the key isn't set.

Full setup instructions, including how to create the key: https://github.com/Yuuzulight/Rozetta#getting-an-api-key
