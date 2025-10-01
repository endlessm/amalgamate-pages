#!/usr/bin/env python3
import argparse
import os
import csv

from godoctopus import GitHubApi, setup_logging, API


ENDLESS_ACCESS_AND_FRIENDS = {
    # For reasons unknown, some of us are anonymous in the upstream repo but not
    # anonymous in other forks. For instance, heather is only listed by her real
    # email address in /repos/endlessm/threadbare/contributors, and only by her
    # GitHub login in some other fork. Baffling.
    "heather@endlessaccess.org",
    "108750056+hydrolet@users.noreply.github.com",
    "stephen@endlessaccess.org",
    "177228389+PlayMatters@users.noreply.github.com"
    "joana@endlessaccess.org",
    "51095367+jofilizola@users.noreply.github.com",

    # Others
    "11432672+JuanFdS@users.noreply.github.com",
    "164198892+jgbourque@users.noreply.github.com",
    "178826543+Stregoica777@users.noreply.github.com",
    "2048532+pablitar@users.noreply.github.com",
    "49699333+dependabot[bot]@users.noreply.github.com",
    "54726643+Arebuayon@users.noreply.github.com",
    "611168+cassidyjames@users.noreply.github.com",
    "6495518+dbnicholson@users.noreply.github.com",
    "68404504+PhoenixStroh@users.noreply.github.com",
    "83944+manuq@users.noreply.github.com",
    "86760+wjt@users.noreply.github.com",
}

def get_contributors(api, repo):
    emails = set()
    for c in api.paginate(f"{API}/repos/{repo}/contributors", {"anon": "true"}):
        if "email" in c:
            emails.add(c["email"])
        else:
            email = f"{c['id']}+{c['login']}@users.noreply.github.com"
            emails.add(email)
    return emails


def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="GitHub Stats")
    parser.add_argument("csv", help="Output from https://useful-forks.github.io/", type=argparse.FileType("r"))
    args = parser.parse_args()

    api_token = os.environ["GITHUB_TOKEN"]
    api = GitHubApi(api_token)

    upstream_contributors = get_contributors(api, "endlessm/threadbare")
    downstream_contributors = set()

    repo_csv = csv.DictReader(args.csv)
    for row in repo_csv:
        downstream_contributors |= get_contributors(api, row["Repo"])

    third_party_upstream = upstream_contributors - ENDLESS_ACCESS_AND_FRIENDS
    print("Upstream contributions:", len(third_party_upstream))

    only_downstream = downstream_contributors - upstream_contributors - ENDLESS_ACCESS_AND_FRIENDS
    print("Downstream contributions:", len(only_downstream))



if __name__ == "__main__":
    main()
