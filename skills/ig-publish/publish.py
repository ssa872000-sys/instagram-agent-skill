#!/usr/bin/env python3
"""
publish.py - publish to Instagram through the official Content Publishing API,
one post at a time or from a scheduled queue, with an approval gate that a
cron job cannot skip.

This is the one tool in the pack that touches the network, and it only ever
talks to Meta's own Graph API. No browser automation, no password, no
third-party service. It needs an Instagram Professional (Business or Creator)
account and a long-lived access token from a Meta developer app with the
publishing permission. SKILL.md walks through getting one.

Everything is a dry run until you pass --live. A dry run prints the exact API
calls it would make and changes nothing.

Environment
  IG_ACCESS_TOKEN   long-lived token (required for anything live)
  IG_USER_ID        the Instagram professional account id (or "me")
  IG_GRAPH_HOST     graph.instagram.com (Instagram Login, default) or
                    graph.facebook.com (Facebook Login, Page-linked account)
  IG_API_VERSION    default v23.0

Usage
  python3 publish.py whoami
  python3 publish.py limit
  python3 publish.py post image https://.../photo.jpg --caption-file cap.txt
  python3 publish.py post reel  https://.../clip.mp4  --caption-file cap.txt --live
  python3 publish.py post carousel https://.../1.jpg https://.../2.jpg --caption "..."
  python3 publish.py post story https://.../story.jpg --live
  python3 publish.py queue add queue.json reel https://.../clip.mp4 \\
          --at 2026-11-03T18:00+06:00 --caption-file cap.txt
  python3 publish.py queue list queue.json
  python3 publish.py queue approve queue.json 3
  python3 publish.py queue run queue.json --live      # put this in cron

Media must sit at a public https URL Instagram can fetch. It does not accept
uploads from your disk on this endpoint.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 5          # Instagram's per-post cap since 18 Dec 2025.
CAROUSEL_MIN, CAROUSEL_MAX = 2, 10
POLL_SECONDS = 5
POLL_TIMEOUT = 600         # Reels can take a few minutes to process.
TYPES = ("image", "reel", "carousel", "story")
VIDEO_EXT = (".mp4", ".mov")

HASHTAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- config / api

def config():
    return {
        "token": os.environ.get("IG_ACCESS_TOKEN", ""),
        "user": os.environ.get("IG_USER_ID", "me"),
        "host": os.environ.get("IG_GRAPH_HOST", "graph.instagram.com"),
        "version": os.environ.get("IG_API_VERSION", "v23.0"),
    }


class Api:
    def __init__(self, cfg, live):
        self.cfg = cfg
        self.live = live
        self.fake = 0
        if live and not cfg["token"]:
            raise ApiError("IG_ACCESS_TOKEN is not set. See SKILL.md, 'Getting a token'.")

    def url(self, path):
        return "https://%s/%s/%s" % (self.cfg["host"], self.cfg["version"], path.lstrip("/"))

    def call(self, method, path, params=None):
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        shown = {k: (v if len(str(v)) < 70 else str(v)[:67] + "...") for k, v in params.items()}
        if not self.live:
            print("  [dry run] %s %s %s" % (method, self.url(path), json.dumps(shown, ensure_ascii=False)))
            self.fake += 1
            return {"id": "DRYRUN_%d" % self.fake, "status_code": "FINISHED"}
        params["access_token"] = self.cfg["token"]
        data = urllib.parse.urlencode(params).encode()
        if method == "GET":
            req = urllib.request.Request(self.url(path) + "?" + data.decode())
        else:
            req = urllib.request.Request(self.url(path), data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                err = json.loads(body).get("error", {})
                msg = "%s (code %s%s)" % (err.get("message", body), err.get("code"),
                                          ", subcode %s" % err["error_subcode"] if err.get("error_subcode") else "")
            except ValueError:
                msg = body[:300]
            raise ApiError("HTTP %d from Instagram: %s" % (e.code, msg)) from None
        except urllib.error.URLError as e:
            raise ApiError("Network error: %s" % e.reason) from None

    # ------------------------------------------------------------ endpoints

    def whoami(self):
        fields = "id,username,account_type,media_count,followers_count"
        path = self.cfg["user"] if self.cfg["user"] != "me" else "me"
        return self.call("GET", path, {"fields": fields})

    def limit(self):
        return self.call("GET", "%s/content_publishing_limit" % self.cfg["user"],
                         {"fields": "config,quota_usage"})

    def container(self, **params):
        return self.call("POST", "%s/media" % self.cfg["user"], params)["id"]

    def wait(self, cid):
        if not self.live:
            return
        start = time.time()
        while True:
            st = self.call("GET", cid, {"fields": "status_code,status"})
            code = st.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise ApiError("Container %s is %s: %s" % (cid, code, st.get("status", "")))
            if time.time() - start > POLL_TIMEOUT:
                raise ApiError("Container %s still %s after %ds" % (cid, code, POLL_TIMEOUT))
            time.sleep(POLL_SECONDS)

    def publish(self, cid):
        return self.call("POST", "%s/media_publish" % self.cfg["user"], {"creation_id": cid})["id"]

    def permalink(self, media_id):
        if not self.live:
            return "(dry run, no permalink)"
        return self.call("GET", media_id, {"fields": "permalink"}).get("permalink", "")


# ---------------------------------------------------------------- validation

def is_video(url):
    return urllib.parse.urlparse(url).path.lower().endswith(VIDEO_EXT)


def validate(kind, media, caption):
    """Return a list of problems. Empty list means it can go."""
    problems = []
    if kind not in TYPES:
        problems.append("type must be one of %s" % ", ".join(TYPES))
    for m in media:
        if not m.startswith("https://"):
            problems.append("media must be a public https:// URL, got %r" % m)
    if kind in ("image", "reel", "story") and len(media) != 1:
        problems.append("%s takes exactly one media URL, got %d" % (kind, len(media)))
    if kind == "carousel" and not CAROUSEL_MIN <= len(media) <= CAROUSEL_MAX:
        problems.append("carousel takes %d-%d items, got %d" % (CAROUSEL_MIN, CAROUSEL_MAX, len(media)))
    if kind == "image" and media and is_video(media[0]):
        problems.append("that looks like a video; use type 'reel'")
    if kind == "reel" and media and not is_video(media[0]):
        problems.append("reel needs a .mp4 or .mov URL")
    if len(caption) > CAPTION_LIMIT:
        problems.append("caption is %d characters, limit is %d" % (len(caption), CAPTION_LIMIT))
    tags = len(HASHTAG_RE.findall(caption))
    if tags > HASHTAG_LIMIT:
        problems.append("%d hashtags, Instagram's cap is %d" % (tags, HASHTAG_LIMIT))
    if "{{" in caption:
        problems.append("caption still has a {{placeholder}} in it")
    if kind == "story" and caption:
        problems.append("stories have no caption through the API; drop it or put the text on the image")
    return problems


# ---------------------------------------------------------------- publishing

def publish_post(api, kind, media, caption="", cover=None, share_to_feed=True):
    problems = validate(kind, media, caption)
    if problems:
        raise ApiError("Not publishing:\n  - " + "\n  - ".join(problems))

    if kind == "image":
        cid = api.container(image_url=media[0], caption=caption)
    elif kind == "reel":
        cid = api.container(media_type="REELS", video_url=media[0], caption=caption,
                            cover_url=cover, share_to_feed="true" if share_to_feed else "false")
    elif kind == "story":
        key = "video_url" if is_video(media[0]) else "image_url"
        cid = api.container(media_type="STORIES", **{key: media[0]})
    else:
        children = []
        for m in media:
            if is_video(m):
                child = api.container(media_type="VIDEO", video_url=m, is_carousel_item="true")
            else:
                child = api.container(image_url=m, is_carousel_item="true")
            api.wait(child)
            children.append(child)
        cid = api.container(media_type="CAROUSEL", children=",".join(children), caption=caption)

    api.wait(cid)
    media_id = api.publish(cid)
    return media_id, api.permalink(media_id)


# ---------------------------------------------------------------- queue

def parse_time(s):
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.astimezone()   # naive means the machine's local time
    return t


def load_queue(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["posts"] if isinstance(data, dict) else data


def save_queue(path, posts):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"posts": posts}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def find(posts, pid):
    for p in posts:
        if str(p["id"]) == str(pid):
            return p
    raise ApiError("no post with id %s in the queue" % pid)


def queue_cmd(args):
    posts = load_queue(args.file)
    now = dt.datetime.now().astimezone()

    if args.action == "add":
        caption = read_caption(args)
        problems = validate(args.type, args.media, caption)
        if problems:
            raise ApiError("Not queued:\n  - " + "\n  - ".join(problems))
        at = parse_time(args.at) if args.at else now
        pid = max([int(p["id"]) for p in posts] or [0]) + 1
        posts.append({"id": pid, "type": args.type, "media": args.media, "caption": caption,
                      "cover": args.cover, "at": at.isoformat(timespec="minutes"),
                      "approved": bool(args.approved), "status": "pending"})
        save_queue(args.file, posts)
        print("queued #%d %s for %s%s" % (pid, args.type, at.isoformat(timespec="minutes"),
                                          "" if args.approved else "  (NOT approved yet: queue approve %s %d)" % (args.file, pid)))
        return 0

    if args.action == "list":
        if not posts:
            print("queue is empty")
        for p in sorted(posts, key=lambda p: p["at"]):
            flag = "ok " if p.get("approved") else "-- "
            first = (p.get("caption") or "").split("\n")[0][:50]
            print("%4s  %s %-9s %-8s %s  %s" % (p["id"], flag, p["status"], p["type"], p["at"], first))
            if p.get("permalink"):
                print("      %s" % p["permalink"])
            if p.get("error"):
                print("      error: %s" % p["error"])
        return 0

    if args.action in ("approve", "unapprove", "remove"):
        p = find(posts, args.id)
        if args.action == "remove":
            posts.remove(p)
        else:
            if args.action == "approve":
                problems = validate(p["type"], p["media"], p.get("caption", ""))
                if problems:
                    raise ApiError("Fix it before approving:\n  - " + "\n  - ".join(problems))
            p["approved"] = args.action == "approve"
        save_queue(args.file, posts)
        done = {"approve": "approved", "unapprove": "unapproved", "remove": "removed"}
        print("%s #%s" % (done[args.action], args.id))
        return 0

    # run
    api = Api(config(), args.live)
    due = [p for p in posts if p["status"] in ("pending", "error")
           and p.get("approved") and parse_time(p["at"]) <= now
           and p.get("attempts", 0) < args.max_attempts]
    waiting = [p for p in posts if p["status"] == "pending" and not p.get("approved")
               and parse_time(p["at"]) <= now]
    for p in waiting:
        print("skip #%s: due but not approved" % p["id"])
    if not due:
        print("nothing due")
        return 0
    failed = 0
    for p in sorted(due, key=lambda p: p["at"])[: args.max]:
        print("publishing #%s %s (%s)" % (p["id"], p["type"], p["at"]))
        try:
            media_id, link = publish_post(api, p["type"], p["media"], p.get("caption", ""),
                                          p.get("cover"))
            if args.live:
                p.update(status="published", media_id=media_id, permalink=link,
                         published_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
                p.pop("error", None)
            print("  done %s" % link)
        except ApiError as e:
            failed += 1
            if args.live:
                p.update(status="error", error=str(e), attempts=p.get("attempts", 0) + 1)
            print("  FAILED: %s" % e, file=sys.stderr)
        if args.live:
            save_queue(args.file, posts)   # save after each one so a crash never double-posts
    return 1 if failed else 0


# ---------------------------------------------------------------- cli

def read_caption(args):
    if getattr(args, "caption_file", None):
        with open(args.caption_file, encoding="utf-8") as f:
            return f.read().strip()
    return (getattr(args, "caption", None) or "").strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish to Instagram via the official API.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="check the token and show the account")
    sub.add_parser("limit", help="show how much of the 24h publishing quota is used")

    def media_args(p):
        p.add_argument("type", choices=TYPES)
        p.add_argument("media", nargs="+", help="public https URLs")
        p.add_argument("--caption", default="")
        p.add_argument("--caption-file")
        p.add_argument("--cover", help="reel cover image URL")

    p = sub.add_parser("post", help="publish one post now")
    media_args(p)
    p.add_argument("--no-feed", action="store_true", help="reel: Reels tab only, not the grid")
    p.add_argument("--live", action="store_true", help="actually publish (default is a dry run)")

    q = sub.add_parser("queue", help="scheduled queue")
    qs = q.add_subparsers(dest="action", required=True)
    a = qs.add_parser("add")
    a.add_argument("file")
    media_args(a)
    a.add_argument("--at", help="ISO time, e.g. 2026-11-03T18:00+06:00 (default: now)")
    a.add_argument("--approved", action="store_true", help="approve on add")
    qs.add_parser("list").add_argument("file")
    for name in ("approve", "unapprove", "remove"):
        x = qs.add_parser(name)
        x.add_argument("file")
        x.add_argument("id")
    r = qs.add_parser("run", help="publish what is due and approved")
    r.add_argument("file")
    r.add_argument("--live", action="store_true")
    r.add_argument("--max", type=int, default=3, help="most posts per run (default 3)")
    r.add_argument("--max-attempts", type=int, default=3)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "queue":
            return queue_cmd(args)
        live = getattr(args, "live", False) or args.cmd in ("whoami", "limit")
        api = Api(config(), live)
        if args.cmd == "whoami":
            print(json.dumps(api.whoami(), indent=2, ensure_ascii=False))
        elif args.cmd == "limit":
            print(json.dumps(api.limit(), indent=2, ensure_ascii=False))
        else:
            if not args.live:
                print("DRY RUN. Nothing will be posted. Add --live to publish.")
            media_id, link = publish_post(api, args.type, args.media, read_caption(args),
                                          args.cover, not args.no_feed)
            print("published %s %s" % (media_id, link))
        return 0
    except ApiError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
