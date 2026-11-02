---
name: ig-publish
description: >-
  Publish to Instagram and schedule posts through the official Content
  Publishing API - images, Reels, carousels and stories, one at a time or from
  a queue that a cron job runs. Use when the user says "post this", "publish
  it", "schedule this for Tuesday", "automate posting", "автопостинг",
  "запланировать пост", or wants the drafts from the other skills to go out
  without opening the app.
---

# ig-publish

The rest of the pack writes. This is the one skill that ships, and it ships
only two ways: through Meta's own Graph API, and only after the user said yes.
No browser automation, no password, no third-party poster. Those are what get
accounts action-blocked, and nothing in this skill does them.

The tool is `publish.py` in this folder. Standard library only. Every command
is a **dry run** until `--live` is passed: it prints the exact API calls and
changes nothing. Always dry-run first and show the user the output.

## What it can and cannot do

Can: publish a single image, a Reel (mp4/mov, cover optional, grid or Reels tab
only), a carousel of 2-10 images and/or videos, and a story. Schedule any of
those in a JSON queue and publish them when they come due.

Cannot, because the API does not allow it: post from a personal account, add
music from the Instagram library, tag products, use filters or stickers, upload
from disk (media must be at a public `https://` URL), follow, like, or DM
strangers. If the user wants those, the answer is the app, not a workaround.

Quota: Instagram caps API publishing per rolling 24 hours. Check it with
`python3 publish.py limit` instead of trusting a number written here.

## Getting a token (once, about 20 minutes)

Walk the user through this only if `IG_ACCESS_TOKEN` is not already set.

1. Switch the Instagram account to **Professional** (Business or Creator):
   Settings > Account type and tools.
2. Go to developers.facebook.com, create an app, add the **Instagram** product,
   choose **API setup with Instagram login**.
3. Add the account as an Instagram tester under App roles, accept the invite in
   the Instagram app, then generate a token with the
   `instagram_business_basic` and `instagram_business_content_publish`
   permissions.
4. Exchange it for a long-lived token (about 60 days) and refresh it before it
   expires. Meta's docs for the current exchange endpoint are the source of
   truth here.
5. Set the environment:

   ```bash
   export IG_ACCESS_TOKEN="..."
   export IG_USER_ID="..."           # from: python3 publish.py whoami
   # Facebook Login with a linked Page instead? also:
   # export IG_GRAPH_HOST=graph.facebook.com
   ```

6. `python3 publish.py whoami` must print the right username before anything
   else happens.

The token is a password. Keep it in the environment or a secrets manager, never
in the queue file, a caption, a commit or the chat.

## The flow

1. The post comes from another skill: `/ig-reel`, `/ig-carousel`,
   `/ig-caption`. Run the caption through `/ig-human` and `caption.py` first.
   `publish.py` refuses a caption over 2,200 characters, over 5 hashtags, or
   with a `{{placeholder}}` still in it.
2. The media is uploaded somewhere public (the user's own site, S3, Cloudinary,
   any CDN). Ask for the URLs. Never invent them.
3. Dry run, show the user, then ask: **"Publish this now?"** Only a plain yes
   counts. Then the same command with `--live`.

```bash
python3 publish.py post reel https://cdn.example.com/clip.mp4 \
  --caption-file caption.txt --cover https://cdn.example.com/cover.jpg
python3 publish.py post carousel https://.../1.jpg https://.../2.jpg --caption-file caption.txt
python3 publish.py post story https://.../story.jpg
```

## Scheduling

The queue is a JSON file, default `~/.claude/instagram/queue.json`. Posts go in
unapproved unless the user says otherwise, and `run` skips anything unapproved
even when it is due. That is the gate, and a cron job cannot open it.

```bash
Q=~/.claude/instagram/queue.json
python3 publish.py queue add $Q reel https://.../clip.mp4 --at 2026-11-03T18:00+06:00 --caption-file cap.txt
python3 publish.py queue list $Q
python3 publish.py queue approve $Q 1          # only after the user approves #1
python3 publish.py queue run $Q                # dry run of what is due
```

Then the cron line, every 15 minutes:

```
*/15 * * * * cd /path/to/skills/ig-publish && IG_ACCESS_TOKEN=... IG_USER_ID=... python3 publish.py queue run ~/.claude/instagram/queue.json --live >> ~/.claude/instagram/publish.log 2>&1
```

`run` publishes at most 3 posts per run (`--max`), saves after each one so a
crash never double-posts, and retries a failed post up to 3 times
(`--max-attempts`) before leaving it as `error` for a human to look at.

For the week from `/ig-plan`, add each slot to the queue with its time, list
it back to the user as one table, and approve only the ones they confirm.

## When it fails

Read the error out loud, it is usually one of these:

- **Media download failed / 9004** - the URL is not public, redirects, or needs
  a login. Open it in a private window.
- **Unsupported format** - Reels want H.264/HEVC mp4 or mov, 9:16, 3-90
  seconds for the Reels tab; images want JPEG.
- **Token expired / code 190** - generate a new long-lived token.
- **Permission error / code 10 or 200** - the app is missing
  `instagram_business_content_publish`, or the account is not Professional.
- **Rate limit** - `publish.py limit`, then wait. Do not loop.

Never retry by changing the caption or the media without telling the user.
After a live publish, append the permalink to `~/.claude/instagram/log.md` so
`/ig-audit` and `/ig-plan` can see it.
