import argparse
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone

import requests
import yaml
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
from munch import munchify

# load config from main.yaml into Munch object, exit if the file is not found
try:
    with open("config.yaml", "r") as file:
        config = munchify(yaml.safe_load(file))
except FileNotFoundError:
    print("Config file not found")
    exit(1)

# set file constants
SOURCE_EXTRALIST_FILE = os.path.join(config.source.dir, config.source.extralist)
SOURCE_FIXLIST_FILE = os.path.join(config.source.dir, config.source.fixlist)
SOURCE_SKIPLIST_FILE = os.path.join(config.source.dir, config.source.skiplist)
DATA_SOURCE_FILE = os.path.join(config.data.dir, config.data.source)
DATA_SUMMARY_FILE = os.path.join(config.data.dir, config.data.summary)
DATA_ERRORS_FILE = os.path.join(config.data.dir, config.data.errors)
OUTPUT_YEAR_FILE = os.path.join(config.output.dir, config.output.year)


# define enum for status
class Status:
    EMPTY_SOURCE = "EMPTY_SOURCE"
    PLAYTIME_MISSING = "PLAYTIME_MISSING"
    PLAYTIME_TOO_HIGH = "PLAYTIME_TOO_HIGH"
    OK = "OK"


# get arguments from argparser
# argument year is required
parser = argparse.ArgumentParser()
parser.add_argument("--year", type=int, required=True, help="Year to process")
args = parser.parse_args()

# initialize logger, set output to console
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)


def fetch_discussion(from_id=None):
    # construct the base URL for the request
    url = f"{config.nyx.api_url}/{config.nyx.discussion_id}?{config.nyx.query_base}"
    # add the query_previous to URL if from_id is set
    url += config.nyx.query_previous.format(from_id=from_id) if from_id else ""
    logger.info(f"Fetching {url}")

    try:
        response = requests.get(url)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        exit(1)

    return response.json()


def convert_html_to_plaintext(text):
    # replace <br> with newlines
    text = text.replace("<br>", "\n")
    # remove all remaining HTML tags
    text = BeautifulSoup(text, "html.parser").get_text()
    # strip blank lines
    text = "\n".join([line for line in text.split("\n") if line.strip()])

    return text


def find_source_line(text):
    # find the first line that contains #dohrano or #dohráno
    for line in text.split("\n"):
        if "#dohrano" in line or "#dohráno" in line:
            return line

    return None


def get_source_parts(text):
    if text is None:
        return None

    # split text into parts by pipes, backslashes or slashes (by default), trim whitespaces
    # remove #dohrano or #dohráno from parts
    separator = "|" if "|" in text else "\\" if "\\" in text else "/"
    parts = [
        part.replace("#dohrano", "").replace("#dohráno", "").strip()
        for part in text.split(separator)
    ]

    # remove parts that are empty
    parts = [part for part in parts if part]

    return parts


def convert_parts_to_data(parts):
    if parts is None:
        return None

    data = []

    # iterate through the parts to determine the type of each part
    for index, part in enumerate(parts):
        # if this is the first part, assume it's a name of a game
        if index == 0:
            type = "game"
            value = part

        else:
            # if the first character is a digit, assume it's a playtime
            if part[0].isdigit():
                # get the pure number from the part:
                # there might be a dot or comma in the middle of the part
                # there might be an unit at the end of the part
                # there are various units
                # unit might not be separated by a space
                # (e.g. "5hod" "1h" "3 hodiny" "2,5 hod" "6.5h")
                value = re.match("^(\d*[,\.]?\d*)", part).group(1)
                # replace comma with dot
                value = value.replace(",", ".")
                # try to convert value to an float
                try:
                    type = "playtime"
                    value = float(value)
                except ValueError:
                    type = "error"
                    value = None

            # else it's probably a platform
            else:
                type = "platform"
                value = part

        data.append(
            {
                "type": type,
                "original": part,
                "value": value,
            }
        )

    # if there is no playtime in the data, try some magic
    if get_status(data) == Status.PLAYTIME_MISSING:
        # if there is a platform in the data, assume it can be a playtime
        # maybe in some weird format, e.g. "pres 35 hodin"
        for part in data:
            if part["type"] == "platform" and "hod" in unicodedata.normalize(
                "NFKD", part["value"]
            ):
                for chunk in part["value"].split(" "):
                    try:
                        value = re.match("^(\d*[,\.]?\d*)", chunk).group(1)
                        value = value.replace(",", ".")
                        part["type"] = "playtime"
                        part["value"] = float(value)
                        break
                    except ValueError:
                        part["type"] = "platform"
                        pass

    return data


def convert_extra_to_data(extra):
    data = []

    # iterate through the extra and create a data structure
    for field in ["id", "username", "inserted_at", "game", "platform", "playtime"]:
        if field in extra:
            data.append(
                {
                    "type": field,
                    "original": "",
                    "value": extra[field],
                }
            )

    return data


def convert_fix_to_data(fix):
    data = []

    # iterate through the fix and create a data structure
    for field in ["game", "platform", "playtime"]:
        if field in fix:
            data.append(
                {
                    "type": field,
                    "original": "",
                    "value": fix[field],
                }
            )

    return data


def get_status(data):
    if data is None:
        return Status.EMPTY_SOURCE

    # check if there is a playtime in the data
    playtime = [part for part in data if part["type"] == "playtime"]
    if not playtime:
        return Status.PLAYTIME_MISSING

    # check that playtime is lower than config.playtime_max
    if playtime[0]["value"] > config.playtime_max:
        return Status.PLAYTIME_TOO_HIGH

    return Status.OK


def main():
    # set the minimum and maximum date of the posts using datetime
    date_min = datetime(args.year, 1, 1, tzinfo=timezone.utc)
    date_max = datetime(args.year + 1, 1, 1, tzinfo=timezone.utc)
    logger.info(f"Date range: {date_min} - {date_max}")

    # create the data and output directory if they doesn't exist
    os.makedirs(config.data.dir, exist_ok=True)
    os.makedirs(config.output.dir, exist_ok=True)
    os.makedirs(config.source.dir, exist_ok=True)

    # set data and output variables
    data_source_file = DATA_SOURCE_FILE.format(year=args.year)
    data_summary_file = DATA_SUMMARY_FILE.format(year=args.year)
    data_errors_file = DATA_ERRORS_FILE.format(year=args.year)
    output_year_file = OUTPUT_YEAR_FILE.format(year=args.year)

    # load extralist from YAML file into Munch object, use emtpy list if the file is not found
    try:
        with open(SOURCE_EXTRALIST_FILE, "r") as file:
            extralist = munchify(yaml.safe_load(file))
    except FileNotFoundError:
        extralist = []

    # load fixlist from YAML file into Munch object, use emtpy list if the file is not found
    try:
        with open(SOURCE_FIXLIST_FILE, "r") as file:
            fixlist = munchify(yaml.safe_load(file))
    except FileNotFoundError:
        fixlist = []
    # create a dictionary from fixlist with post id as key
    fixlist_by_id = {fix.id: fix for fix in fixlist}

    # load skiplist from YAML file into Munch object, use emtpy list if the file is not found
    try:
        with open(SOURCE_SKIPLIST_FILE, "r") as file:
            skiplist = munchify(yaml.safe_load(file))
    except FileNotFoundError:
        skiplist = []

    # set empty data variables
    data_source_by_username = {}
    data_summary_by_username = {}
    data_errors = []

    # TODO: refactor this to a function
    # iterate over extralist and append data to data_source_by_username
    for extra in extralist:
        logger.info(f"Added extra post {extra.id}")

        source_data = convert_extra_to_data(extra)
        status = get_status(source_data)

        data = {
            "id": extra.id,
            "status": status,
            "url": config.nyx.post_url.format(
                discussion_id=config.nyx.discussion_id, post_id=extra.id
            ),
            "username": extra.username,
            "inserted_at": extra.inserted_at,
            "content": "",
            "source_line": "",
            "source_parts": [],
            "source_data": source_data,
        }

        if extra.username in data_source_by_username:
            data_source_by_username[extra.username].append(data)
        else:
            data_source_by_username[extra.username] = [data]

        if status != Status.OK:
            data_errors.append(data)

    # fetch the first (newest) page of the discussion
    discussion = fetch_discussion()

    while True:
        for post in discussion["posts"]:
            # TODO: refactor this to a function
            # if inserted_at of the post is lower than date_min or higher than date_max, skip the post
            post_inserted_at = datetime.fromisoformat(post["inserted_at"]).replace(
                tzinfo=timezone.utc
            )
            if post_inserted_at < date_min or post_inserted_at > date_max:
                logger.info(f"Skipping post {post['id']} (not in date range)")
                continue

            # transform the post content to source data and get the status
            content = convert_html_to_plaintext(post["content"])
            source_line = find_source_line(content)
            source_parts = get_source_parts(source_line)
            source_data = convert_parts_to_data(source_parts)
            status = get_status(source_data)

            # if the status is not OK, decide what to do with the post
            if status != Status.OK:
                # if the post is in the skiplist, skip the post
                if post["id"] in skiplist:
                    logger.info(f"Skipping post {post['id']} (in skiplist)")
                    continue
                # if the post is in the fixlist, use the fixlist data
                if post["id"] in fixlist_by_id:
                    source_data = convert_fix_to_data(fixlist_by_id[post["id"]])
                    status = get_status(source_data)

            # log the status of the post
            logger.info(f"Post {post['id']} {status}")

            # prepare data structure
            data = {
                "id": post["id"],
                "status": status,
                "url": config.nyx.post_url.format(
                    discussion_id=config.nyx.discussion_id, post_id=post["id"]
                ),
                "username": post["username"],
                "inserted_at": post["inserted_at"],
                "content": content,
                "source_line": source_line,
                "source_parts": source_parts,
                "source_data": source_data,
            }

            # append data to data_source_by_username
            if post["username"] in data_source_by_username:
                data_source_by_username[post["username"]].append(data)
            else:
                data_source_by_username[post["username"]] = [data]

            # if status is not OK, append data to data_errors
            if status != Status.OK:
                data_errors.append(data)

        # get the last post
        last_post = discussion["posts"][-1]

        # if inserted_at of the last_post is lower than date_min, break the loop
        last_post_inserted_at = datetime.fromisoformat(
            last_post["inserted_at"]
        ).replace(tzinfo=timezone.utc)
        if last_post_inserted_at < date_min:
            break

        # use the last_post id as the from_id for the next request
        discussion = fetch_discussion(from_id=last_post["id"])

    # iterate over data_source_by_username and create data_summary_by_username
    for username, data in data_source_by_username.items():
        # count the number of posts with status OK
        count = len([post for post in data if post["status"] == Status.OK])
        if count > 0:
            # sum the playtime of posts with status OK
            playtime = sum(
                [
                    [
                        part
                        for part in post["source_data"]
                        if part["type"] == "playtime"
                    ][0]["value"]
                    for post in data
                    if post["status"] == Status.OK
                ]
            )
            # format playtime without decimal places if there is no decimal part
            playtime = int(playtime) if playtime == int(playtime) else playtime

            # create a list of games with status OK
            games = [
                {
                    "name": [
                        part for part in post["source_data"] if part["type"] == "game"
                    ][0]["value"],
                    "playtime": [
                        part
                        for part in post["source_data"]
                        if part["type"] == "playtime"
                    ][0]["value"],
                    "date": datetime.fromisoformat(post["inserted_at"])
                    .replace(tzinfo=timezone.utc)
                    .strftime("%-d.%-m."),
                    "url": post["url"],
                }
                for post in data
                if post["status"] == Status.OK
            ]
            # format playtime without decimal places if there is no decimal part
            for game in games:
                game["playtime"] = (
                    int(game["playtime"])
                    if game["playtime"] == int(game["playtime"])
                    else game["playtime"]
                )

            # append data to data_summary_by_username
            data_summary_by_username[username] = {
                "count": count,
                "playtime": playtime,
                "games": games,
            }

    # sort data_summary_by_username by playtime
    data_summary_by_username = {
        k: v
        for k, v in sorted(
            data_summary_by_username.items(),
            key=lambda item: item[1]["playtime"],
            reverse=True,
        )
    }

    # save data_source_by_username as YAML file
    with open(data_source_file, "w", encoding="utf8") as file:
        yaml.dump(data_source_by_username, file, allow_unicode=True)

    # save data_summary_by_username as YAML file
    with open(data_summary_file, "w", encoding="utf8") as file:
        yaml.dump(data_summary_by_username, file, allow_unicode=True)

    # save data_errors as YAML file
    with open(data_errors_file, "w", encoding="utf8") as file:
        yaml.dump(data_errors, file, allow_unicode=True)

    # create a Jinja2 environment
    env = Environment(
        loader=FileSystemLoader(config.templates.dir), autoescape=select_autoescape()
    )

    # load and render the template
    template = env.get_template(config.templates.main)
    html = template.render(
        summary=data_summary_by_username,
        max_playtime=max(
            [summary["playtime"] for summary in data_summary_by_username.values()]
        ),
        generated_at=datetime.now(timezone.utc).strftime("%-d.%-m.%Y @ %H:%M:%S %Z"),
        years=config.years,
        year_current=args.year,
    )

    # save the rendered template as HTML file
    with open(output_year_file, "w") as file:
        file.write(html)


if __name__ == "__main__":
    main()
