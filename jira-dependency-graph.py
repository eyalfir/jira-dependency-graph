#!/usr/bin/env python

from __future__ import print_function

import argparse
import json
import sys
import getpass

import requests

from collections import OrderedDict

# Using REST is pretty simple. The vast majority of this code is about the "other stuff": dealing with
# command line options, formatting graphviz, calling Google Charts, etc. The actual JIRA REST-specific code
# is only about 5 lines.

GOOGLE_CHART_URL = 'http://chart.apis.google.com/chart?'
MAX_SUMMARY_LENGTH = 30


def log(*args):
    print(*args, file=sys.stderr)


class JiraSearch(object):
    """ This factory will create the actual method used to fetch issues from JIRA. This is really just a closure that saves us having
        to pass a bunch of parameters all over the place all the time. """

    def __init__(self, url, auth):
        self.url = url + '/rest/api/latest'
        self.auth = auth
        self.fields = ','.join(['key', 'summary', 'assignee', 'labels', 'status', 'description', 'issuetype', 'issuelinks', 'subtasks'])

    def get(self, uri, params={}):
        headers = {'Content-Type' : 'application/json'}
        url = self.url + uri

        if isinstance(self.auth, str):
            return requests.get(url, params=params, cookies={'JSESSIONID': self.auth}, headers=headers)
        else:
            return requests.get(url, params=params, auth=self.auth, headers=headers)

    def get_issue(self, key):
        """ Given an issue key (i.e. JRA-9) return the JSON representation of it. This is the only place where we deal
            with JIRA's REST API. """
        log('Fetching ' + key)
        # we need to expand subtasks and links since that's what we care about here.
        response = self.get('/issue/%s' % key, params={'fields': self.fields})
        response.raise_for_status()
        return response.json()

    def query(self, query):
        log('Querying ' + query)
        # TODO comment
        response = self.get('/search', params={'jql': query, 'fields': self.fields})
        content = response.json()
        return content['issues']


def build_graph_data(start_issue_key, jira, excludes, show_directions, directions, includes, ignore_closed, ignore_epic, jq=None, extra_jq=None):
    """ Given a starting image key and the issue-fetching function build up the GraphViz data representing relationships
        between issues. This will consider both subtasks and issue links.
    """
    def get_key(issue):
        return issue['key']

    def create_node_label(issue_key, fields):
        # truncate long labels with "...", but only if the three dots are
        # replacing more than two characters -- otherwise the truncated
        # label would be taking more space than the original.
        summary = fields['summary']
        if len(summary) > MAX_SUMMARY_LENGTH+2:
            summary = summary[:MAX_SUMMARY_LENGTH] + '...'
        short_summary = summary.replace('"', '\\"')
        if not jq:
            return '{} ({})'.format(issue_key, short_summary)
        else:
            import pyjq
            try:
                return str(pyjq.one(jq, fields, vars=dict(issue_key=issue_key)))
                #'"{}({})\n{}\n{}"'.format(issue_key, short_summary, fields['assignee']['displayName'], fields['labels'])
            except Exception:
                log('Error with issue %s' % issue_key)
                print(fields)
                raise


    def process_link(fields, issue_key, link):
        if link.has_key('outwardIssue'):
            direction = 'outward'
        elif link.has_key('inwardIssue'):
            direction = 'inward'
        else:
            return

        if direction not in directions:
            return

        linked_issue_key = get_key(link[direction + 'Issue'])
        link_type = link['type'][direction]

        if ignore_closed:
            if 'inwardIssue' in link:
                log('Verifying linked key is not closed : ' + link['inwardIssue']['fields']['status']['name'])
                if link['inwardIssue']['fields']['status']['name'] in 'Closed':
                    return
            if 'outwardIssue' in link:
                log('Verifying linked key is not closed : ' + link['outwardIssue']['fields']['status']['name'])
                if link['outwardIssue']['fields']['status']['name'] in 'Closed':
                    return

        if includes not in linked_issue_key:
            return


        if link_type in excludes:
            return linked_issue_key, None

        if direction == 'outward':
            log(issue_key + ' => ' + link_type + ' => ' + linked_issue_key)
        else:
            log(issue_key + ' <= ' + link_type + ' <= ' + linked_issue_key)

        extra = ""
        if link_type == "blocks":
            extra = ',color="red"'

        if direction not in show_directions:
            node = None
        else:
            log ("Linked issue " + linked_issue_key)
            node = '"{}"->"{}"[label="{}"{}]'.format(issue_key, linked_issue_key, link_type, extra)


        return linked_issue_key, node

    # since the graph can be cyclic we need to prevent infinite recursion
    seen = []

    def create_node_extra(issue_key, fields):
        import pyjq
        try:
            return pyjq.one(extra_jq, fields, vars=dict(issue_key=issue_key))
        except Exception:
            log('Problem with extra for issue %s' % issue_key)
            print (fields)
            raise

    def walk(issue_key, graph):
        """ issue is the JSON representation of the issue """
        issue = jira.get_issue(issue_key)
        children = []
        fields = issue['fields']
        seen.append(issue_key)

        if ignore_closed:
            log('Verifying issue key ' + issue_key + ' is not closed : ' + issue['fields']['status']['name'])
            if issue['fields']['status']['name'] in 'Closed':
                return graph

        graph.append('"{}" [label="{}"]'.format(issue_key, create_node_label(issue_key, fields)))
        if extra_jq:
            graph.append('"{}" [{}]'.format(issue_key, create_node_extra(issue_key, fields)))

        if fields['issuetype']['name'] == 'Epic' and not ignore_epic:
            issues = jira.query('"Epic Link" = "%s"' % issue_key)
            for subtask in issues:
                subtask_key = get_key(subtask)
                log(subtask_key + ' => references epic => ' + issue_key)
                node = '{}->{}[color=orange]'.format(issue_key, subtask_key)
                graph.append(node)
                children.append(subtask_key)
        if fields.has_key('subtasks'):
            for subtask in fields['subtasks']:
                subtask_key = get_key(subtask)
                log(issue_key + ' => has subtask => ' + subtask_key)
                node = '{}->{}[color=blue][label="subtask"]'.format(issue_key, subtask_key)
                graph.append(node)
                children.append(subtask_key)
        if fields.has_key('issuelinks'):
            for other_link in fields['issuelinks']:
                result = process_link(fields, issue_key, other_link)
                if result is not None:
                    log('Appending ' + result[0])
                    children.append(result[0])
                    if result[1] is not None:
                        graph.append(result[1])
        # now construct graph data for all subtasks and links of this issue
        for child in (x for x in children if x not in seen):
            walk(child, graph)
        return graph

    return walk(start_issue_key, [])


def create_graph_image(graph_data, image_file):
    """ Given a formatted blob of graphviz chart data[1], make the actual request to Google
        and store the resulting image to disk.

        [1]: http://code.google.com/apis/chart/docs/gallery/graphviz.html
    """
    chart_url = GOOGLE_CHART_URL + 'cht=gv&chl=digraph{%s}' % ';'.join(graph_data)

    print('Google Chart request:')
    print(chart_url)

    response = requests.get(chart_url)

    with open(image_file, 'w+') as image:
        print('Writing to ' + image_file)
        image.write(response.content)

    return image_file


def print_graph(graph_data):
    print('digraph{%s}' % ';\n'.join(graph_data))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-u', '--user', dest='user', default=None, help='Username to access JIRA')
    parser.add_argument('-p', '--password', dest='password', default=None, help='Password to access JIRA')
    parser.add_argument('-c', '--cookie', dest='cookie', default=None, help='JSESSIONID session cookie value')
    parser.add_argument('-j', '--jira', dest='jira_url', default='http://jira.example.com', help='JIRA Base URL')
    parser.add_argument('-f', '--file', dest='image_file', default='issue_graph.png', help='Filename to write image to')
    parser.add_argument('-l', '--local', action='store_true', default=False, help='Render graphviz code to stdout')
    parser.add_argument('-e', '--ignore-epic', action='store_true', default=False, help='Don''t follow an Epic into it''s children issues')
    parser.add_argument('-x', '--exclude-link', dest='excludes', default=[], action='append', help='Exclude link type(s)')
    parser.add_argument('--ignore-closed', dest='closed', action='store_true', default=False, help='Ignore closed issues')
    parser.add_argument('-i', '--issue-include', dest='includes', default='', help='Include issue keys')
    parser.add_argument('-F', '--format', dest='jq', default='', help='Use this jq pattern to format nodes labels')
    parser.add_argument('-N', '--node-format', dest='extra_jq', default='', help='Use this jq pattern to format nodes')
    parser.add_argument('-s', '--show-directions', dest='show_directions', default=['inward', 'outward'], help='which directions to show (inward,outward)')
    parser.add_argument('-d', '--directions', dest='directions', default=['inward', 'outward'], help='which directions to walk (inward,outward)')
    parser.add_argument('issues', nargs='+', help='The issue key (e.g. JRADEV-1107, JRADEV-1391)')

    return parser.parse_args()

def filter_duplicates(lst):
    # Enumerate the list to restore order lately; reduce the sorted list; restore order
    def append_unique(acc, item):
        return acc if acc[-1][1] == item[1] else acc.append(item) or acc
    srt_enum = sorted(enumerate(lst), key=lambda (i, val): val)
    return [item[1] for item in sorted(reduce(append_unique, srt_enum, [srt_enum[0]]))]

def main():
    options = parse_args()

    if options.cookie is not None:
        # Log in with browser and use --cookie=ABCDEF012345 commandline argument
        auth = options.cookie
    else:
        # Basic Auth is usually easier for scripts like this to deal with than Cookies.
        user = options.user if options.user is not None \
                    else raw_input('Username: ')
        password = options.password if options.password is not None \
                    else getpass.getpass('Password: ')
        auth = (user, password)

    jira = JiraSearch(options.jira_url, auth)

    graph = []
    for issue in options.issues:
        graph = graph + build_graph_data(issue, jira, options.excludes, options.show_directions, options.directions, options.includes, options.closed, options.ignore_epic, jq=options.jq, extra_jq=options.extra_jq)

    if options.local:
        print_graph(filter_duplicates(graph))
    else:
        create_graph_image(filter_duplicates(graph), options.image_file)

if __name__ == '__main__':
    main()
