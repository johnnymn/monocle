# MIT License
# Copyright (c) 2019 Fabien Boucher

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import logging
import requests
from datetime import datetime
import json
import re
from dataclasses import dataclass


name = 'gerrit_crawler'
help = 'Gerrit Crawler to fetch Reviews events'


@dataclass
class GerritCrawlerArgs(object):
    updated_since: str
    loop_delay: int
    command: str
    index: str
    base_url: str
    repository: str


class ReviewesFetcher(object):

    log = logging.getLogger(__name__)

    def __init__(self, base_url, repository_prefix):
        self.base_url = base_url
        self.repository_prefix = repository_prefix
        self.status_map = {'NEW': 'OPEN', 'MERGED': 'MERGED', 'ABANDONED': 'CLOSED'}
        self.message_re = re.compile(r"Patch Set \d+:( [^ ]+[+-]\d+)?\n\n.+")

    def convert_date_for_db(self, str_date):
        cdate = datetime.strptime(str_date[:-10], '%Y-%m-%d %H:%M:%S').strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return cdate

    def convert_date_for_query(self, str_date):
        # It looks like Gerrit behaves curiously as no
        # data is returned if there is a TZ marker as well
        # as if seconds are specified. Let's adapt the date str
        # for the query.
        # Even it looks like it does not take in account the %H:%M
        # part ...
        str_date = str_date.replace('T', ' ')
        str_date = str_date.replace('Z', '')
        cdate = datetime.strptime(str_date, '%Y-%m-%d %H:%M:%S').strftime("%Y-%m-%d")
        return cdate

    def get(self, updated_since):
        updated_since = self.convert_date_for_query(updated_since)
        request_params = "?q=after:%s+project:%s" % (
            updated_since,
            self.repository_prefix,
        )
        for option in [
            'MESSAGES',
            'DETAILED_ACCOUNTS',
            'DETAILED_LABELS',
            'CURRENT_REVISION',
            'CURRENT_FILES',
            'CURRENT_COMMIT',
        ]:
            request_params += '&o=%s' % option
        count = 100
        start_after = 0
        reviews = []
        while True:
            urlpath = (
                self.base_url
                + '/changes/'
                + request_params
                + '&n=%s&start=%s' % (count, start_after)
            )
            self.log.info("query: %s" % urlpath)
            response = requests.get(urlpath)
            _reviewes = json.loads(response.text[4:])
            if _reviewes:
                reviews.extend(_reviewes)
                self.log.info("read %s reviews from the api" % len(reviews))
                if reviews[-1].get('_more_changes'):
                    start_after = len(reviews)
                else:
                    break
            else:
                break
        return reviews

    def extract_objects(self, reviewes):
        def timedelta(start, end):
            format = "%Y-%m-%dT%H:%M:%SZ"
            start = datetime.strptime(start, format)
            end = datetime.strptime(end, format)
            return int((start - end).total_seconds())

        def insert_change_attributes(obj, change):
            obj.update(
                {
                    'repository_prefix': change['repository_prefix'],
                    'repository_fullname': change['repository_fullname'],
                    'repository_shortname': change['repository_shortname'],
                    'number': change['number'],
                    'repository_fullname_and_number': change[
                        'repository_fullname_and_number'
                    ],
                    'on_author': change['author'],
                    'on_created_at': change['created_at'],
                }
            )

        def extract_pr_objects(review):
            objects = []
            change = {
                'type': 'Change',
                'id': review['id'],
                'number': review['_number'],
                'target_branch': review['branch'],
                'branch': review['branch'],
                'repository_prefix': review['project'].split('/')[0],
                'repository_fullname': review['project'],
                'repository_shortname': "/".join(review['project'].split('/')[1:]),
                'url': '%s/%s' % (self.base_url, review['_number']),
                'author': "%s/%s"
                % (review['owner'].get('name'), review['owner']['_account_id']),
                'title': review['subject'],
                'updated_at': self.convert_date_for_db(review['updated']),
                'created_at': self.convert_date_for_db(review['created']),
                'merged_at': (
                    self.convert_date_for_db(review.get('submitted'))
                    if review.get('submitted')
                    else None
                ),
                # Note(fbo): The mergeable field is sometime absent
                'mergeable': (True if review.get('mergeable') == 'true' else False),
                'state': self.status_map[review['status']],
                # Note(fbo): Gerrit labels must be handled as Review
                'labels': [],
                # Note(fbo): Only one assignee possible by review on Gerrit
                'assignees': (
                    [
                        "%s/%s"
                        % (
                            review['assignee'].get('name'),
                            review['assignee']['_account_id'],
                        )
                    ]
                    if review.get('assignee')
                    else []
                ),
                'additions': review['insertions'],
                'deletions': review['deletions'],
                # Gerrit review is one commit by review
                'commit_count': 1,
                'changed_files': len(
                    list(review['revisions'].values())[0]['files'].keys()
                ),
                'changes_files_details': [
                    {
                        'additions': details.get('lines_inserted', 0),
                        'deletions': details.get('lines_deleted', 0),
                        'path': path,
                    }
                    for path, details in list(review['revisions'].values())[0][
                        'files'
                    ].items()
                ],
                'text': list(review['revisions'].values())[0]['commit']['message'],
            }
            change['repository_fullname_and_number'] = "%s#%s" % (
                change['repository_fullname'],
                change['number'],
            )
            if change['state'] == 'CLOSED':
                # CLOSED means abandoned in that context
                # use updated_at date as closed_at
                change['closed_at'] = change['updated_at']
            if change['state'] == 'MERGED':
                change['closed_at'] = change['merged_at']
            if change['state'] in ('CLOSED', 'MERGED'):
                change['duration'] = timedelta(
                    change['closed_at'], change['created_at']
                )
            if change['state'] == 'MERGED':
                if "submitter" in review:
                    # Gerrit 2.x seems to not have that submitter attribute
                    change['merged_by'] = "%s/%s" % (
                        review['submitter'].get('name'),
                        review['submitter']['_account_id'],
                    )
            else:
                change['merged_by'] = None
            objects.append(change)
            obj = {
                'type': 'ChangeCreatedEvent',
                'id': 'CCE' + change['id'],
                'created_at': change['created_at'],
                'author': change['author'],
            }
            insert_change_attributes(obj, change)
            objects.append(obj)
            if change['state'] in ('MERGED', 'CLOSED'):
                obj = {
                    'type': 'ChangeMergedEvent'
                    if change['state'] == 'MERGED'
                    else 'ChangeAbandonedEvent',
                    'id': 'CCLE' + change['id'],
                    'created_at': change['closed_at'],
                    # Gerrit does not tell about closed_by so here
                    # let's set None except if merged_by
                    # is set (Gerrit 3.x tells about the author of a merge)
                    'author': change.get('merged_by'),
                }
                insert_change_attributes(obj, change)
                objects.append(obj)
            for comment in review['messages']:
                if comment['message'].startswith('Uploaded patch set '):
                    obj = {
                        'type': 'ChangeCommitPushedEvent',
                        'id': comment['id'],
                        'created_at': self.convert_date_for_db(comment['date']),
                        'author': "%s/%s"
                        % (
                            comment['author'].get('name'),
                            comment['author']['_account_id'],
                        ),
                    }
                    insert_change_attributes(obj, change)
                    objects.append(obj)
                    continue
                # Here we apply a regexp to ensure the message contains a message
                # body. Inline comments match as well because they add in the message
                # body the string '(X comments)'.
                # Gerrit reports votes as comments, this regexp not match if
                # the message only match a vote such as Code-Review+1 w/o further comments
                if self.message_re.match(comment['message']):
                    obj = {
                        'type': 'ChangeCommentedEvent',
                        'id': comment['id'],
                        'created_at': self.convert_date_for_db(comment['date']),
                        'author': "%s/%s"
                        % (
                            comment['author'].get('name'),
                            comment['author']['_account_id'],
                        ),
                    }
                    insert_change_attributes(obj, change)
                    objects.append(obj)
            for label in review['labels']:
                for _review in review['labels'][label].get('all', []):
                    # If the date field exists then it means a review label
                    # has been set by someone
                    if 'date' in _review and 'value' in _review:
                        obj = {
                            'type': 'ChangeReviewedEvent',
                            'id': "%s_%s_%s_%s"
                            % (
                                self.convert_date_for_db(_review['date']),
                                label,
                                _review['value'],
                                _review['_account_id'],
                            ),
                            'created_at': self.convert_date_for_db(_review['date']),
                            'author': "%s/%s"
                            % (_review.get('name'), _review['_account_id']),
                            'approval': "%s%s"
                            % (
                                label,
                                (
                                    "+%s" % _review['value']
                                    if not str(_review['value']).startswith('-')
                                    else _review['value']
                                ),
                            ),
                        }
                        insert_change_attributes(obj, change)
                        objects.append(obj)
            return objects

        objects = []
        for review in reviewes:
            try:
                objects.extend(extract_pr_objects(review))
            except Exception:
                self.log.exception("Unable to extract Review data: %s" % review)
        return objects


if __name__ == "__main__":
    from pprint import pprint

    # rf = ReviewesFetcher('https://gerrit-review.googlesource.com', 'gerrit')
    # reviewes = rf.get('2020-04-08 00:00:00')
    # reviewes = reviewes[:10]

    # rf = ReviewesFetcher('https://review.opendev.org', 'zuul/zuul')
    # reviewes = rf.get('2020-04-08 00:00:00')
    # reviewes = reviewes[:10]

    rf = ReviewesFetcher(
        'https://softwarefactory-project.io/r', 'software-factory/sf-config'
    )
    reviewes = rf.get('2020-04-08 00:00:00')
    reviewes = reviewes[:10]

    pprint(reviewes)
    objs = rf.extract_objects(reviewes)
    pprint(objs)
