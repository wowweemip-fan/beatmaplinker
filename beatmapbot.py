import re
import html
import praw
import requests
import os
import urllib.parse
import time
from spaceconfigparser import ConfigParser
from functools import lru_cache
from limitedset import LimitedSet
from functools import reduce


if not os.path.exists("config.ini"):
    print("No config file found.")
    print("Copy config_example.ini to config.ini and modify to your needs.")
    exit()

config = ConfigParser()
config.read("config.ini")
template_extras = ConfigParser()
template_extras.read("template_extras.ini")

MAX_COMMENTS = int(config.get("bot", "max_comments"))
MAX_SUBMISSIONS = int(config.get("bot", "max_submissions"))
OSU_CACHE = int(config.get("bot", "osu_cache"))
URL_REGEX = re.compile(r'<a href="(?P<url>https?://osu\.ppy\.sh/[^"]+)">(?P=url)</a>')  # NOQA


@lru_cache(maxsize=OSU_CACHE)
def get_beatmap_info(map_type, map_id):
    """Gets information about a beatmap given a type and id.

    Cached helper function to try to minimize osu! api requests.
    """
    payload = {"k": config.get("osu", "api_key"), map_type: map_id}
    r = requests.get("https://osu.ppy.sh/api/get_beatmaps", params=payload)
    out = r.json()
    if "error" in out:
        raise Exception("osu!api returned an error of " + out["error"])
    return out


def seconds_to_string(seconds):
    """Returns a m:ss representation of a time in seconds."""
    return "{0}:{1:0>2}".format(*divmod(seconds, 60))


def get_map_params(url):
    """Returns a tuple of (map_type, map_id) or False if URL is invalid.

    Possible URL formats:
        https://osu.ppy.sh/p/beatmap?b=115891&m=0#
        https://osu.ppy.sh/b/244182
        https://osu.ppy.sh/p/beatmap?s=295480
        https://osu.ppy.sh/s/295480
    """
    parsed = urllib.parse.urlparse(url)

    map_type, map_id = None, None
    if parsed.path.startswith("/b/"):
        map_type, map_id = "b", parsed.path[3:]
    elif parsed.path.startswith("/s/"):
        map_type, map_id = "s", parsed.path[3:]
    elif parsed.path == "/p/beatmap":
        query = urllib.parse.parse_qs(parsed.query)
        if "b" in query:
            map_type, map_id = "b", query["b"][0]
        elif "s" in query:
            map_type, map_id = "s", query["s"][0]
    if "&" in map_id:
        map_id = map_id[:map_id.index("&")]
    if map_type and map_id.isdigit():
        return map_type, map_id
    return False


def sanitise_md(string):
    """Escapes any markdown characters in string."""
    emphasis = "*_"
    escaped = reduce(lambda a, b: a.replace(b, "&#{:0>4};".format(ord(b))),
                     emphasis, string)
    other_chars = list("\\[]^") + ["~~"]
    escaped = reduce(lambda a, b: a.replace(b, "\\" + b), other_chars, escaped)
    return escaped


def format_map(map_type, map_id):
    """Formats a map for a comment given its type and id."""
    map_info = get_beatmap_info(map_type, map_id)
    if not map_info:  # invalid beatmap
        return "Invalid map{}.".format(["", "set"][map_type == "s"])
    info = dict(map_info[0])  # create new instance

    for section in template_extras.sections():
        section_obj = template_extras[section]
        info[section] = section_obj[info[section_obj["_key"]]]

    info["difficultyrating"] = float(info["difficultyrating"])
    info["hit_length"] = seconds_to_string(int(info["hit_length"]))
    info["total_length"] = seconds_to_string(int(info["total_length"]))

    # Sanitised inputs
    for key in ["artist", "creator", "source", "title", "version"]:
        info[key] = sanitise_md(info[key])

    if len(map_info) == 1:  # single map
        return config.get("template", "map").format(**info)
    else:  # beatmap set
        return config.get("template", "mapset").format(**info)


def remove_dups(iterable):
    """Creates a generator to get unique elements from iterable.

    Items in iterable must be hashable.
    """
    seen = set()
    for item in iterable:
        if item not in seen:
            seen.add(item)
            yield item


def format_comment(maps):
    """Formats a list of (map_type, map_id) tuples into a comment."""
    header = config.get("template", "header")
    footer = config.get("template", "footer")
    body = ""
    line_break = "\n\n"
    base_len = len(header) + len(footer) + len(line_break) * 2
    if "sep" in config["template"]:
        sep = config.get("template", "sep").replace("\\n", "\n")
    else:
        sep = line_break

    for beatmap in remove_dups(maps):
        next_map = format_map(*beatmap)
        if base_len + len(body) + len(sep) + len(next_map) > 10000:
            print("We've reached the char limit! This has", len(maps), "maps.")
            break
        if body:
            body += sep
        body += next_map

    return "{header}{br}{body}{br}{footer}".format(
        header=header, body=body, footer=footer, br=line_break
    )


def get_maps_from_html(html_string):
    """Returns a list of all valid maps as (map_type, map_id) tuples
    from some HTML.
    """
    return list(filter(None, (get_map_params(html.unescape(z))
                              for z in URL_REGEX.findall(html_string))))


def get_maps_from_thing(thing):
    """Returns a list of all valid maps as (map_type, map_id) tuples
    from a thing.
    """
    if isinstance(thing, praw.objects.Comment):
        body_html = thing.body_html
    elif isinstance(thing, praw.objects.Submission):
        if not thing.selftext_html:
            return []
        body_html = thing.selftext_html
    else:
        raise Exception("{0} is an invalid thing type".format(type(thing)))
    return get_maps_from_html(html.unescape(body_html))


def has_replied(thing, r):
    """Checks whether the bot has replied to a thing already.

    Apparently costly.
    Taken from http://www.reddit.com/r/redditdev/comments/1kxd1n/_/cbv4usl"""
    botname = config.get("reddit", "username")
    if isinstance(thing, praw.objects.Comment):
        replies = r.get_submission(thing.permalink).comments[0].replies
    elif isinstance(thing, praw.objects.Submission):
        replies = thing.comments
    else:
        raise Exception("{0} is an invalid thing type".format(type(thing)))
    return any(reply.author.name == botname for reply in replies)


def reply(thing, text):
    """Post a comment replying to a thing."""
    if thing.author.name == config.get("reddit", "username"):
        print("Replying to self. Terminating.")
        return
    print("Replying to {c.author.name}, thing id {c.id}".format(c=thing))
    print()
    print(text)
    print()
    if isinstance(thing, praw.objects.Comment):
        thing.reply(text)
    elif isinstance(thing, praw.objects.Submission):
        thing.add_comment(text)
    else:
        raise Exception("{0} is an invalid thing type".format(type(thing)))
    print("Replied!")


def thing_loop(thing_type, content, seen, r):
    """Scans content for new things to reply to."""
    for thing in content:
        if thing.id in seen:
            break  # already reached up to here before
        seen.add(thing.id)
        found = get_maps_from_thing(thing)
        if not found:
            print("New", thing_type, thing.id, "with no maps.")
            continue
        if has_replied(thing, r):
            print("We've replied to", thing_type, thing.id, "before!")
            break  # we reached here in a past instance of this bot

        reply(thing, format_comment(found))


r = praw.Reddit(user_agent=config.get("reddit", "user_agent"))
r.login(config.get("reddit", "username"), config.get("reddit", "password"))

seen_comments = LimitedSet(MAX_COMMENTS + 100)
seen_submissions = LimitedSet(MAX_SUBMISSIONS + 50)
subreddit = r.get_subreddit(config.get("reddit", "subreddit"))


while True:
    try:
        thing_loop("comment", subreddit.get_comments(limit=MAX_COMMENTS),
                   seen_comments, r)
        thing_loop("submission", subreddit.get_new(limit=MAX_SUBMISSIONS),
                   seen_submissions, r)
        time.sleep(3)
    except KeyboardInterrupt:
        print("Stopping the bot.")
        exit()
    except Exception as e:
        print("We caught an exception! It says:")
        print(e)
        print("Sleeping for 15 seconds.")
        time.sleep(15)
        continue
