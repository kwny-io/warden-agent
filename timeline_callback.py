import datetime

# 建库时间：2026-09-04 08:27:11 UTC
TARGET = datetime.datetime(2026, 9, 4, 8, 27, 11,
                           tzinfo=datetime.timezone.utc)


def callback(commit, ancestors):
    # 只改"根提交"（没有父提交的那条）的时间；其他提交一律不动。
    if not commit.parents:
        commit.author_date = TARGET
        commit.committer_date = TARGET
