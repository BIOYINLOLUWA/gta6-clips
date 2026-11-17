# clip-pipeline

Sourcing, clipping, approval and posting module for **gta6-content-engine**.

```
watchlist ──► discover (leak guard) ──► audio only ──► loudness peaks ──► transcribe peaks
   ──► score + hook + 3 captions (Claude, optional) ──► download only those sections
   ──► render 9:16 (68s TikTok cut + 40s Shorts/Reels cut, Bebas Neue captions)
   ──► Telegram: you tap ✅1 / ✅2 / ✅3 / ❌
   ──► Upload-Post schedules TikTok, YouTube Shorts, Instagram Reels into free slots
```

Your only daily job: tap buttons in Telegram.

## Drop into the repo

```
gta6-content-engine/
├── clip-pipeline/            <- this folder (minus .github)
└── .github/workflows/
    ├── clip-produce.yml      <- move from clip-pipeline/.github/workflows
    └── clip-review.yml
```

## One-time setup (about 30 min)

1. **Telegram bot**: message @BotFather, run `/newbot`, copy the token. Send your bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
   (If the engine's Telegram MCP already has a bot, you can reuse it, but use a separate chat so the approval buttons don't get mixed up with other messages.)
2. **Upload-Post** (upload-post.com): create a profile (e.g. `gta6clips`) and connect TikTok, YouTube and Instagram. Copy your API key.
3. **Postgres**: use the engine's existing database: `psql "$DATABASE_URL" -f clip-pipeline/db/schema.sql`
   (or `python -m pipeline.cli init-db`).
4. **GitHub secrets** (Settings > Secrets > Actions):
   `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER`,
   optional `ANTHROPIC_API_KEY` (better hooks and captions; without it, template captions are used).
5. **Watchlist**: edit `config/watchlist.yaml`. Add each Whop campaign streamer with `campaign:` set and
   `pre_launch_ok: false`. Add your own gameplay channel.
6. Run **clip-produce** manually once (Actions tab > Run workflow) and check that clips arrive in Telegram.

## Safety rails built in

- **Leak guard** runs before download and again before posting. It always blocks leak terms (leak, cyberleek,
  early copy, unboxing, pre-release...). Before 19 Nov, it also blocks any non-official "GTA 6 gameplay" and
  any source not marked `pre_launch_ok`.
- **Nothing posts without your tap.** Every Telegram card reminds you to check for leaks, cutscene/story spoilers and graphic first 7 seconds (YouTube's no-ads zone).
- TikTok cut is 68s (over 60s, so it is eligible for Creator Rewards). Streamer credit is added to YouTube and Instagram posts for campaign clips.

## Things to know

- **GitHub Actions minutes:** a private repo gets 2,000 free minutes/month, and this setup would use roughly 4,000+.
  Either make the repo **public** (standard runners are free, and secrets stay hidden) or add your PC as a
  **self-hosted runner** (free; change `runs-on: ubuntu-latest` to `runs-on: self-hosted`).
- **Launch week:** change the produce cron to `*/2` and `--max-sources` to 5.
- **Whop submissions are manual:** after a campaign clip goes live, paste its link into the campaign dashboard.
  The scheduled post IDs are stored in `clips.post_response`.
- **Telegram as storage:** clips are kept under 19MB so runners can re-download them from Telegram. No S3 needed.
- Posting slots (UK time) are in `pipeline/post.py` `SLOTS`. Upload-Post handles the scheduled publish.

## Commands

```
python -m pipeline.cli produce --max-sources 3   # find + render + send for review
python -m pipeline.cli review                    # read taps + schedule
python -m pipeline.cli status                    # counts
python -m pytest -q                              # 20 tests incl. real ffmpeg renders
```

## Upgrade ideas (not built)

- Twitch chat-spike scoring (strongest virality signal for streamer VODs)
- Nightly view stats into Postgres via Upload-Post analytics to tune the scorer
- Instagram-specific fields (cover frame, share-to-feed) once confirmed in Upload-Post docs
