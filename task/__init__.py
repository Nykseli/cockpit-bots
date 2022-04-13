# This file is part of Cockpit.
#
# Copyright (C) 2017 Red Hat, Inc.
#
# Cockpit is free software; you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# Cockpit is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Cockpit; If not, see <http://www.gnu.org/licenses/>.

# Shared GitHub code. When run as a script, we print out info about
# our GitHub interacition.

import argparse
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import traceback

from . import github
from . import sink
from lib.constants import BASE_DIR


__all__ = (
    "api",
    "main",
    "run",
    "pull",
    "comment",
    "label",
    "issue",
    "verbose",
    "default_branch",
)

sys.dont_write_bytecode = True

api = github.GitHub()
verbose = False


#
# The main function takes a list of tasks, each of wihch has the following
# fields, some of which have defaults:
#
#   title: The title for the task
#   function: The function to call for the task
#   options=[]: A list of string options to pass to the function argument
#
# The function for the task will be called with all the context for the task.
# In addition it will be called with named arguments for all other task fields
# and additional fields such as |verbose|. It should return a zero or None value
# if successful, and a string or non-zero value if unsuccessful.
#
#   def run(context, verbose=False, **kwargs):
#       if verbose:
#           sys.stderr.write(image + "\n")
#       return 0
#
# Call the task.main() as the entry point of your script in one of these ways:
#
#   # As a single task
#   task.main(title="My title", function=run)
#


def main(**kwargs):
    global verbose

    task = kwargs.copy()

    # Figure out a descriptoin for the --help
    task["name"] = named(task)

    parser = argparse.ArgumentParser(description=task.get("title", task["name"]))
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--issue", dest="issue", action="store",
                        help="Act on an already created task issue")
    parser.add_argument("--publish", dest="publish", default=os.environ.get("TEST_PUBLISH", ""),
                        action="store", help="Publish results centrally to a sink")
    parser.add_argument("--dry", dest="dry", action="store_true",
                        help="Dry run to validate this task if supported")
    parser.add_argument("context", nargs="?")

    opts = parser.parse_args()
    verbose = opts.verbose

    ret = 0

    if "verbose" not in task:
        task["verbose"] = opts.verbose
    task["issue"] = opts.issue
    task["publish"] = opts.publish
    task["dry"] = opts.dry

    ret = run(opts.context, **task)

    if ret:
        sys.stderr.write("{0}: {1}\n".format(task["name"], ret))

    sys.exit(ret and 1 or 0)


def named(task):
    if "name" in task:
        return task["name"]
    else:
        return os.path.basename(os.path.realpath(sys.argv[0]))


def begin(publish, name, context, issue):
    if not publish:
        return None

    hostname = socket.gethostname().split(".")[0]
    current = time.strftime('%Y%m%d-%H%M%M')

    # Update the body for an existing issue
    if issue:
        number = issue["number"]
        identifier = "{0}-{1}-{2}".format(name, number, current)
        title = issue["title"]
        wip = "WIP: {0}: [no-test] {1}".format(hostname, title)
        requests = [{
            "method": "POST",
            "resource": api.qualify("issues/{0}".format(number)),
            "data": {"title": wip}
        }, {
            "method": "POST",
            "resource": api.qualify("issues/{0}/comments".format(number)),
            "data": {"body": "{0} in progress on {1}.\nLog: :link".format(name, hostname)}
        }]
        watches = [{
            "resource": api.qualify("issues/{0}".format(number)),
            "result": {"title": wip}
        }]
        aborted = [{
            "method": "POST",
            "resource": api.qualify("issues/{0}".format(number)),
            "data": {"title": title}
        }, {
            "method": "POST",
            "resource": api.qualify("issues/{0}/comments".format(number)),
            "data": {"body": "Task aborted."}
        }]
    else:
        identifier = "{0}-{1}".format(name, current)
        requests = []
        watches = []
        aborted = []

    status = {
        "github": {
            "token": api.token,
            "requests": requests,
            "watches": watches
        },

        "onaborted": {
            "github": {
                "token": api.token,
                "requests": aborted
            }
        }
    }

    publishing = sink.Sink(publish, identifier, status)
    sys.stderr.write("# Task: {0} {1}\n# Host: {2}\n\n".format(name, context or "", hostname))

    # For statistics
    publishing.start = time.time()

    return publishing


def finish(publishing, ret, name, context, issue):
    if not publishing:
        return

    if not ret:
        comment = None
        result = "Completed"
    elif isinstance(ret, str):
        comment = "{0}: :link".format(ret)
        result = ret
    else:
        comment = "Task failed: :link"
        result = "Failed"

    duration = int(time.time() - publishing.start)
    sys.stderr.write("\n# Result: {0}\n# Duration: {1}s\n".format(result, duration))

    if issue:
        # Note that we check whether pass or fail ... this is because
        # the task is considered "done" until a human comes through and
        # triggers it again by unchecking the box.
        item = "{0} {1}".format(name, context or "").strip()
        checklist = github.Checklist(issue["body"])
        checklist.check(item, ret and "FAIL" or True)

        number = issue["number"]

        # The sink wants us to escape colons :S
        body = checklist.body.replace(':', '::')

        requests = [{
            "method": "POST",
            "resource": api.qualify("issues/{0}".format(number)),
            "data": {"title": "{0}".format(issue["title"]), "body": body}
        }]

        # Close the issue if it's not a pull request, successful, and all tasks done
        if "pull_request" not in issue and not ret and len(checklist.items) == len(checklist.checked()):
            requests[0]["data"]["state"] = "closed"

        # Comment if there was a failure
        if comment:
            requests.insert(0, {
                "method": "POST",
                "resource": api.qualify("issues/{0}/comments".format(number)),
                "data": {"body": comment}
            })

    else:
        requests = []

    publishing.status['github']['requests'] = requests
    publishing.status['github']['watches'] = None
    publishing.status['github']['onaborted'] = None
    publishing.flush()


def run(context, function, **kwargs):
    number = kwargs.get("issue", None)
    publish = kwargs.get("publish", "")
    name = kwargs["name"]

    issue = None
    if number:
        issue = api.get("issues/{0}".format(number))
        if not issue:
            return "No such issue: {0}".format(number)
        elif issue["title"].startswith("WIP:"):
            return "Issue is work in progress: {0}: {1}\n".format(number, issue["title"])
        kwargs["issue"] = issue
        kwargs["title"] = issue["title"]

    publishing = begin(publish, name, context, issue=issue)

    ret = "Task threw an exception"
    try:
        if issue and "pull_request" in issue:
            kwargs["pull"] = api.get(issue["pull_request"]["url"])

        ret = function(context, **kwargs)
    except (RuntimeError, subprocess.CalledProcessError) as ex:
        ret = str(ex)
    except (AssertionError, KeyboardInterrupt):
        raise
    except Exception:
        traceback.print_exc()
    finally:
        finish(publishing, ret, name, context, issue)
    return ret or 0


def issue(title, body, item, context=None, items=[], state="open", since=None):
    if context:
        item = "{0} {1}".format(item, context).strip()

    if since:
        # don't let all bots pass the deadline in the same second, to avoid many duplicates
        since += random.randint(-3600, 3600)

    for issue in api.issues(state=state, since=since):
        checklist = github.Checklist(issue["body"])
        if item in checklist.items:
            return issue

    if not items:
        items = [item]
    checklist = github.Checklist(body)
    for x in items:
        checklist.add(x)
    data = {
        "title": title,
        "body": checklist.body,
        "labels": ["bot"]
    }
    return api.post("issues", data)


def execute(*args):
    global verbose
    if verbose:
        sys.stderr.write("+ " + " ".join(args) + "\n")

    # Make double sure that the token does not appear anywhere in the output
    def censored(text):
        return text.replace(api.token, "CENSORED")

    env = os.environ.copy()
    # No prompting for passwords
    if "GIT_ASKPASS" not in env:
        env["GIT_ASKPASS"] = "/bin/true"
    try:
        output = subprocess.check_output(args, cwd=BASE_DIR, stderr=subprocess.STDOUT, env=env, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        if verbose:
            sys.stderr.write("! " + str(e))
        raise
    sys.stderr.write(censored(output))
    return output


def push_branch(branch, force=False):
    cmd = ["git", "push", api.remote, "+HEAD:refs/heads/{0}".format(branch)]
    if force:
        cmd.insert(2, "-f")
    execute(*cmd)


def branch(context, message, pathspec=".", issue=None, push=True, **kwargs):
    name = named(kwargs)

    execute("git", "checkout", "--detach")
    current = time.strftime('%Y%m%d-%H%M%M')
    branch = "{0} {1} {2}".format(name, context or "", current).strip()
    branch = branch.replace(" ", "-").replace("--", "-")

    # Tell git about our github token for authentication
    try:
        subprocess.check_call(["git", "config", "credential.https://github.com.username", api.token])
    except subprocess.CalledProcessError:
        raise RuntimeError("Couldn't configure git config with our API token")

    clean = "https://github.com/{0}".format(api.repo)

    if pathspec is not None:
        execute("git", "add", "--", pathspec)

    # If there's nothing to add at that pathspec return None
    try:
        execute("git", "commit", "-m", message)
    except subprocess.CalledProcessError:
        return None

    # No need to push if we want to add another commits into the same branch
    if push:
        push_branch(branch)

    # Comment on the issue if present and we pushed the branch
    if issue and push:
        comment_done(issue, name, clean, branch, context)

    return branch


def pull(branch, body=None, issue=None, base=None, labels=['bot'], run_tests=True, **kwargs):
    if "pull" in kwargs:
        return kwargs["pull"]

    # $GITHUB_REF is set when running from workflows
    if not base:
        base = os.path.basename(os.getenv("GITHUB_REF", default_branch()))

    data = {
        "head": branch,
        "base": base,
        "maintainer_can_modify": True
    }
    if issue:
        try:
            data["issue"] = issue["number"]
        except TypeError:
            data["issue"] = int(issue)
    else:
        data["title"] = "[no-test] " + kwargs["title"]
        if body:
            data["body"] = body

    try:
        pull = api.post("pulls", data)
    except RuntimeError as e:
        # If we were refused to grant maintainer_can_modify, then try without
        if "fork_collab" in e.data:
            data["maintainer_can_modify"] = False
            pull = api.post("pulls", data)
        else:
            raise e

    # Update the pull request
    label(pull, labels)

    # Update the issue if it is a dict
    if issue:
        try:
            issue["title"] = kwargs["title"]
            issue["pull_request"] = {"url": pull["url"]}
        except TypeError:
            pass

    if pull["number"]:
        # If we want to run tests automatically, drop [no-test] from title before force push
        if run_tests:
            pull = api.post("pulls/" + str(pull["number"]), {"title": kwargs["title"]}, accept=[422])

        # Force push
        last_commit_m = execute("git", "show", "--no-patch", "--format=%B")
        last_commit_m += "Closes #" + str(pull["number"])
        execute("git", "commit", "--amend", "-m", last_commit_m)
        push_branch(branch, force=True)

        # Make sure we return the updated pull data
        for retry in range(20):
            new_data = api.get("pulls/{}".format(pull["number"]))
            if pull["head"]["sha"] != new_data["head"]["sha"]:
                pull = new_data
                break
            time.sleep(6)
        else:
            raise RuntimeError("Failed to retrieve updated pull data after force pushing")

    return pull


def label(issue, labels=['bot']):
    try:
        resource = "issues/{0}/labels".format(issue["number"])
    except TypeError:
        resource = "issues/{0}/labels".format(issue)
    return api.post(resource, labels)


def labels_of_pull(pull):
    if "labels" not in pull:
        pull["labels"] = api.get("issues/{0}/labels".format(pull["number"]))
    return list(map(lambda label: label["name"], pull["labels"]))


def comment(issue, comment):
    try:
        number = issue["number"]
    except TypeError:
        number = issue
    return api.post("issues/{0}/comments".format(number), {"body": comment})


def comment_done(issue, name, clean, branch, context=None):
    message = "{0} {1} done: {2}/commits/{3}".format(name, context or "", clean, branch)
    comment(issue, message)


def attach(filename):
    if "TEST_ATTACHMENTS" in os.environ:
        shutil.copy(filename, os.environ["TEST_ATTACHMENTS"])


def default_branch():
    '''Returns the default branch of a repository

    The default branch should be used as a default base.
    '''

    return api.get()["default_branch"]
