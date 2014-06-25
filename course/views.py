from django.shortcuts import (  # noqa
        render, get_object_or_404, redirect)
from django.contrib import messages
import django.forms as forms

import datetime

from course.models import (
        Course, Participation,
        participation_role, participation_status)
from course.content import (
        get_git_repo, get_course_desc, parse_date_spec,
        get_flow
        )

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit


def get_role_and_participation(request, course):
    participations = Participation.objects.filter(
            user=request.user, course=course)

    if len(participations) > 1:
        messages.add_message(request, messages.WARNING,
                "Multiple enrollments found. Please contact the course staff.")

    if len(participations) == 0:
        return participation_role.unenrolled, None

    participation = participations[0]
    if participation.status != participation_status.active:
        return participation_role.unenrolled, participation
    else:
        return participation.role, participation


class AccessResult:
    def __init__(self, what, credit_percent):
        self.what = what
        self.credit_percent = credit_percent

        assert what in ["allow", "view", "deny"]


def get_flow_access(course_desc, role, flow, flow_visit):
    if flow_visit is not None:
        now = flow_visit.start_time.date()
    else:
        now = datetime.datetime.now().date()

    for rule in flow.access_rules:
        if role not in rule.roles:
            continue
        if hasattr(rule, "start"):
            start_date = parse_date_spec(course_desc, rule.start)
            if now < start_date:
                continue
        if hasattr(rule, "end"):
            end_date = parse_date_spec(course_desc, rule.end)
            if end_date < now:
                continue

        return AccessResult(rule.access, getattr(rule, "credit_percent", 100))

    return AccessResult("deny", 100)


def find_flow_visit(role, participation):
    return None


# {{{ views

def home(request):
    courses_and_descs = []
    for course in Course.objects.all():
        courses_and_descs.append(
                (course, get_course_desc(request, course)))

    courses_and_descs.sort(key=lambda (course, desc): desc.course_start)

    return render(request, "course/home.html", {
        "courses_and_descs": courses_and_descs
        })


def course_page(request, course_identifier):
    course = get_object_or_404(Course, identifier=course_identifier)

    course_desc = get_course_desc(request, course)

    role, participation = get_role_and_participation(request, course)

    return render(request, "course/course-page.html", {
        "course": course,
        "course_desc": course_desc,
        "participation": participation,
        "role": role,
        "participation_role": participation_role,
        })


class StartForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.helper = FormHelper()
        self.helper.form_class = "form-horizontal"

        self.helper.add_input(
                Submit("submit", "Get started"))
        super(StartForm, self).__init__(*args, **kwargs)


def start_flow(request, course_identifier, flow_identifier):
    course = get_object_or_404(Course, identifier=course_identifier)

    role, participation = get_role_and_participation(request, course)

    # TODO: Could be one of multiple
    fvisit = find_flow_visit(role, participation)
    if fvisit:
        active_git_commit_sha = fvisit.active_git_commit_sha
    else:
        active_git_commit_sha = course.active_git_commit_sha

    course_desc = get_course_desc(request, course)

    flow = get_flow(request, course, flow_identifier, active_git_commit_sha)

    access = get_flow_access(course_desc, role, flow, None)

    if access.what == "deny":
        messages.add_message(request, messages.WARNING,
                "Access denied")
        return render(request, "course/blank.html",
                {
                    "course": course,
                    "course_desc": course_desc,
                    },
                status=403)

    if request.method == "POST":
        if role != role.unenrolled:
            fvisit = FlowVisit()
            fvisit.participation = participation
            fvisit.active_git_commit_sha = course.active_git_commit_sha

    return render(request, "course/flow-start-page.html", {
        "course": course,
        "course_desc": course_desc,
        "flow": flow,
        "form": StartForm(),
        })


def view_flow_page(request, course_identifier, flow_identifier, page_identifier):
    course = get_object_or_404(Course, identifier=course_identifier)
    course_desc = get_course_desc(request, course)

    return render(request, "course/flow-page.html", {
        "course": course,
        "course_desc": course_desc,
        #"flow_desc": flow_desc,
        })


# {{{ git interaction

def pull_course_updates(request, course_identifier):
    import sys

    course = get_object_or_404(Course, identifier=course_identifier)
    course_desc = get_course_desc(request, course)

    was_successful = True
    log_lines = []
    try:
        repo = get_git_repo(course)

        if not course.git_source:
            raise RuntimeError("no git source URL specified")

        if course.ssh_private_key:
            repo.auth(pkey=course.ssh_private_key.encode("ascii"))

        log_lines.append("Pre-pull head is at '%s'" % repo.head)
        repo.pull(course.git_source.encode("utf-8"))
        log_lines.append("Post-pull head is at '%s'" % repo.head)

    except Exception:
        was_successful = False
        from traceback import format_exception
        log = "\n".join(log_lines) + "".join(
                format_exception(*sys.exc_info()))
    else:
        log = "\n".join(log_lines)

    return render(request, 'course/course-bulk-result.html', {
        "process_description": "Pull course updates via git",
        "log": log,
        "status": "Pull successful."
            if was_successful
            else "Pull failed. See above for error.",
        "was_successful": was_successful,
        "course": course,
        "course_desc": course_desc,
        })


class GitUpdateForm(forms.Form):
    new_sha = forms.CharField(required=True, initial=50)

    def __init__(self, *args, **kwargs):
        self.helper = FormHelper()
        self.helper.form_class = "form-horizontal"
        self.helper.label_class = "col-lg-2"
        self.helper.field_class = "col-lg-8"

        self.helper.add_input(
                Submit("submit", "Validate and update", css_class="col-lg-offset-2"))
        super(GitUpdateForm, self).__init__(*args, **kwargs)


def update_course(request, course_identifier):
    course = get_object_or_404(Course, identifier=course_identifier)
    course_desc = get_course_desc(request, course)

    repo = get_git_repo(course)

    if request.method == "POST":
        form = GitUpdateForm(request.POST, request.FILES)
        if form.is_valid():
            new_sha = form.cleaned_data["new_sha"].encode("utf-8")

            from course.content import validate_course_content
            try:
                validate_course_content(course, new_sha)
            except Exception, e:
                messages.add_message(request, messages.ERROR,
                        "Course content did not validate successfully. (%s) "
                        "Update not applied."
                        % str(e))
            else:
                messages.add_message(request, messages.INFO,
                        "Course content validated successfully. Update applied.")
                course.active_git_commit_sha = new_sha
                course.save()
    else:
        form = GitUpdateForm({
            "new_sha": repo.head
            })

    text_lines = [
            "<b>Active git SHA:</b> %s (%s)" % (
                course.active_git_commit_sha,
                repo[course.active_git_commit_sha.encode("utf-8")].message),
            "<b>Current git HEAD:</b> %s (%s)" % (
                repo.head,
                repo[repo.head].message,
            )
            ]

    return render(request, "course/generic-course-form.html", {
        "form": form,
        "form_text": "".join(
            "<p>%s</p>" % line
            for line in text_lines
            ),
        "form_description": "Update Course Revision",
        "course": course,
        "course_desc": course_desc,
    })

# }}}

# }}}

# vim: foldmethod=marker