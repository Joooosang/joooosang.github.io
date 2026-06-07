import argparse
import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

import pandas as pd


SERIES_ID = "tt0239195"
GRAPHQL_URL = "https://caching.graphql.imdb.com/"
OUTPUT_EXCEL = "imdb_survivor_episode_reviews.xlsx"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    ),
    "x-imdb-client-name": "imdb-web-next-localized",
    "Accept-Language": "en-US,en;q=0.9",
}


def graphql(query, variables=None, max_retries=4):
    payload = {"query": query, "variables": variables or {}}
    body = json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries):
        request = Request(GRAPHQL_URL, data=body, headers=HEADERS, method="POST")
        try:
            with urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("errors"):
                raise RuntimeError(data["errors"])
            return data["data"]
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if attempt == max_retries - 1:
                raise RuntimeError(f"HTTP {error.code}: {detail[:500]}") from error
        except Exception:
            if attempt == max_retries - 1:
                raise
        time.sleep(2**attempt)


def get_seasons(series_id):
    query = """
    query Seasons($id: ID!) {
      title(id: $id) {
        episodes {
          displayableSeasons(first: 100) {
            edges {
              node {
                season
                text
              }
            }
          }
        }
      }
    }
    """
    data = graphql(query, {"id": series_id})
    edges = data["title"]["episodes"]["displayableSeasons"]["edges"]
    seasons = [edge["node"]["season"] for edge in edges]
    return sorted(seasons, key=lambda value: int(value) if value.isdigit() else 10**9)


def get_episodes_for_season(series_id, season):
    query = """
    query Episodes($id: ID!, $season: String!, $after: ID) {
      title(id: $id) {
        episodes {
          episodes(first: 25, after: $after, filter: {includeSeasons: [$season]}) {
            total
            pageInfo {
              hasNextPage
              endCursor
            }
            edges {
              node {
                id
                titleText {
                  text
                }
                series {
                  displayableEpisodeNumber {
                    displayableSeason {
                      season
                      text
                    }
                    episodeNumber {
                      episodeNumber
                      text
                    }
                  }
                }
                ratingsSummary {
                  aggregateRating
                  voteCount
                }
                reviews(first: 1) {
                  total
                }
              }
            }
          }
        }
      }
    }
    """
    episodes = []
    after = None
    while True:
        data = graphql(query, {"id": series_id, "season": season, "after": after})
        conn = data["title"]["episodes"]["episodes"]
        for edge in conn["edges"]:
            node = edge["node"]
            display_no = node["series"]["displayableEpisodeNumber"]
            episodes.append(
                {
                    "episode_id": node["id"],
                    "season": display_no["displayableSeason"]["season"],
                    "episode": display_no["episodeNumber"]["episodeNumber"],
                    "episode_title": node["titleText"]["text"],
                    "episode_imdb_rating": node.get("ratingsSummary", {}).get("aggregateRating"),
                    "episode_vote_count": node.get("ratingsSummary", {}).get("voteCount"),
                    "episode_review_count": node.get("reviews", {}).get("total", 0),
                }
            )

        page_info = conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
        time.sleep(0.2)

    return episodes


def get_episode_reviews(episode_id):
    query = """
    query Reviews($id: ID!, $after: ID) {
          title(id: $id) {
        reviews(first: 25, after: $after, sort: {by: SUBMISSION_DATE, order: DESC}) {
          total
          pageInfo {
            hasNextPage
            endCursor
          }
          edges {
            node {
              id
              authorRating
              spoiler
              submissionDate
              summary {
                originalText
              }
              text {
                originalText {
                  plainText
                }
              }
              helpfulness {
                upVotes
                downVotes
                score
              }
            }
          }
        }
      }
    }
    """
    reviews = []
    after = None
    while True:
        try:
            data = graphql(query, {"id": episode_id, "after": after})
        except RuntimeError as error:
            if "BAD_USER_INPUT" in str(error):
                return reviews
            raise
        conn = data["title"]["reviews"]
        for edge in conn["edges"]:
            review = edge["node"]
            text = ((review.get("text") or {}).get("originalText") or {}).get("plainText")
            reviews.append(
                {
                    "review_id": review.get("id"),
                    "review_date": review.get("submissionDate"),
                    "user_rating": review.get("authorRating"),
                    "spoiler": review.get("spoiler"),
                    "review_title": (review.get("summary") or {}).get("originalText"),
                    "review_text": text,
                    "helpful_up_votes": (review.get("helpfulness") or {}).get("upVotes"),
                    "helpful_down_votes": (review.get("helpfulness") or {}).get("downVotes"),
                    "helpfulness_score": (review.get("helpfulness") or {}).get("score"),
                }
            )

        page_info = conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
        time.sleep(0.2)
    return reviews


def score_text_sentiment(text):
    text = (text or "").lower()
    positive_words = {
        "great",
        "good",
        "excellent",
        "amazing",
        "best",
        "love",
        "loved",
        "enjoy",
        "enjoyed",
        "fun",
        "interesting",
        "perfect",
        "strong",
        "iconic",
        "legendary",
        "fantastic",
        "solid",
    }
    negative_words = {
        "bad",
        "boring",
        "worst",
        "awful",
        "terrible",
        "poor",
        "hate",
        "hated",
        "disappointing",
        "weak",
        "slow",
        "dull",
        "predictable",
        "annoying",
        "mess",
        "waste",
    }
    words = re.findall(r"[a-z']+", text)
    score = sum(word in positive_words for word in words) - sum(word in negative_words for word in words)
    if score > 0:
        return "positive"
    if score < 0:
        return "negative"
    return "neutral"


def classify_sentiment(rating, text):
    if pd.notna(rating):
        rating = int(rating)
        if rating >= 7:
            return "positive"
        if rating <= 4:
            return "negative"
        return "neutral"
    return score_text_sentiment(text)


def main(series_id=SERIES_ID, output_excel=OUTPUT_EXCEL):
    seasons = get_seasons(series_id)
    print(f"시즌 {len(seasons)}개 확인: {', '.join(seasons[:10])} ... {seasons[-1]}", flush=True)

    episodes = []
    for season in seasons:
        season_episodes = get_episodes_for_season(series_id, season)
        episodes.extend(season_episodes)
        print(f"시즌 {season}: 회차 {len(season_episodes)}개", flush=True)
        time.sleep(0.2)

    rows = []
    total_episodes = len(episodes)
    for index, episode in enumerate(episodes, start=1):
        if not episode.get("episode_review_count"):
            print(
                f"{index}/{total_episodes} "
                f"S{episode['season']}E{episode['episode']} {episode['episode_id']}: 리뷰 0개",
                flush=True,
            )
            continue

        reviews = get_episode_reviews(episode["episode_id"])
        for review in reviews:
            row = {**episode, **review}
            row["sentiment"] = classify_sentiment(row.get("user_rating"), row.get("review_text"))
            rows.append(row)
        print(
            f"{index}/{total_episodes} "
            f"S{episode['season']}E{episode['episode']} {episode['episode_id']}: 리뷰 {len(reviews)}개"
            ,
            flush=True,
        )
        time.sleep(0.25)

    columns = [
        "season",
        "episode",
        "episode_id",
        "episode_title",
        "episode_imdb_rating",
        "episode_vote_count",
        "episode_review_count",
        "review_id",
        "review_date",
        "user_rating",
        "sentiment",
        "spoiler",
        "review_title",
        "review_text",
        "helpful_up_votes",
        "helpful_down_votes",
        "helpfulness_score",
    ]
    df = pd.DataFrame(rows, columns=columns)
    if not df.empty:
        df["season"] = pd.to_numeric(df["season"], errors="coerce")
        df["episode"] = pd.to_numeric(df["episode"], errors="coerce")
        df["review_date"] = pd.to_datetime(df["review_date"], errors="coerce")
        df = df.sort_values(["season", "episode", "review_date"], ascending=[True, True, False])

    df.to_excel(output_excel, index=False)
    print(df.shape)
    print(df.head(10).to_string(index=False))
    print(f"저장 완료: {output_excel}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--series-id", default=SERIES_ID)
    parser.add_argument("--output", default=OUTPUT_EXCEL)
    args = parser.parse_args()
    main(series_id=args.series_id, output_excel=args.output)
