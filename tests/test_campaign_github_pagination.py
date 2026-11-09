"""Repository identity and query scope survive GitHub's numeric pagination links."""

import pytest

from bmad_loop.campaign_github import ApiResponse, GithubError, GithubGateway

ROOT = "repos/a/b"
QUERY = "state=open&sort=updated&direction=asc&per_page=100"
FIRST = f"{ROOT}/issues?{QUERY}"


class Transport:
    def __init__(self, link):
        self.calls = []
        self.responses = {
            ROOT: ApiResponse(200, {"id": 777, "full_name": "a/b"}, {}),
            FIRST: ApiResponse(200, [{"id": 1}], {"link": link}),
        }

    def request(self, method, path, payload=None):
        assert method == "GET" and payload is None
        self.calls.append(path)
        # An unsafe request would succeed: refusal must come from the gateway,
        # not an incidental missing fake response or schema error.
        return self.responses.get(path, ApiResponse(200, [{"id": 999}], {}))


def header(path):
    return f'<https://api.github.com/{path}>; rel="next"'


def test_numeric_repository_links_retain_cursor_scope_and_all_pages():
    second_query = QUERY + "&after=Y3Vyc29y%3D&page=2"
    third_query = "page=3&" + QUERY + "&after=bmV4dA%3D%3D"
    transport = Transport(header("repositories/777/issues?" + second_query))
    transport.responses[f"{ROOT}/issues?{second_query}"] = ApiResponse(
        200,
        [{"id": 2}, {"id": 3, "pull_request": {}}],
        {"link": header("repositories/777/issues?" + third_query)},
    )
    transport.responses[f"{ROOT}/issues?{third_query}"] = ApiResponse(200, [{"id": 4}], {})
    assert [row["id"] for row in GithubGateway(transport, "a/b").issues()] == [1, 2, 4]
    assert transport.calls == [
        FIRST,
        ROOT,
        f"{ROOT}/issues?{second_query}",
        f"{ROOT}/issues?{third_query}",
    ]


def test_numeric_alias_preserves_nested_collection_endpoint():
    transport = Transport("")
    first = ROOT + "/commits/abc/check-runs?filter=all&per_page=100"
    second = first + "&page=2"
    transport.responses[first] = ApiResponse(
        200,
        {"check_runs": [{"id": 1}]},
        {"link": header("repositories/777/commits/abc/check-runs?filter=all&per_page=100&page=2")},
    )
    transport.responses[second] = ApiResponse(200, {"check_runs": [{"id": 2}]}, {})
    assert GithubGateway(transport, "a/b").pages(
        ROOT + "/commits/abc/check-runs?filter=all", collection="check_runs"
    ) == [{"id": 1}, {"id": 2}]


@pytest.mark.parametrize(
    "link",
    [
        f"https://evil.test/{ROOT}/issues?{QUERY}&page=2",
        f"http://api.github.com/{ROOT}/issues?{QUERY}&page=2",
        f"https://api.github.com.evil.test/{ROOT}/issues?{QUERY}&page=2",
        f"https://name@api.github.com/{ROOT}/issues?{QUERY}&page=2",
        f"https://api.github.com:443/{ROOT}/issues?{QUERY}&page=2",
        f"https://api.github.com/repos/a/other/issues?{QUERY}&page=2",
        f"https://api.github.com/repositories/778/issues?{QUERY}&page=2",
        f"https://api.github.com/{ROOT}/pulls?{QUERY}&page=2",
        f"https://api.github.com/repositories/777/pulls?{QUERY}&page=2",
        f"https://api.github.com/{ROOT}/../other/issues?{QUERY}&page=2",
        f"https://api.github.com/{ROOT}/%2e%2e/other/issues?{QUERY}&page=2",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&page=2#fragment",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&page=2\n",
        "https://[invalid",
        f'https://api.github.com/{ROOT}/issues?{QUERY.replace("state=open", "state=closed")}&page=2',
        f'https://api.github.com/{ROOT}/issues?{QUERY.replace("per_page=100", "per_page=10")}&page=2',
        f"https://api.github.com/{ROOT}/issues?page=2",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&page=2&page=3",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&page=0",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&page=invalid",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&page",
        f"https://api.github.com/{ROOT}/issues?{QUERY}&unexpected=1&page=2",
    ],
)
def test_unsafe_next_page_is_refused_before_fetch(link):
    transport = Transport(f'<{link}>; rel="next"')
    with pytest.raises(GithubError):
        GithubGateway(transport, "a/b").issues()
    assert transport.calls in ([FIRST], [FIRST, ROOT])


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {},
        {"id": True, "full_name": "a/b"},
        {"id": "777", "full_name": "a/b"},
        {"id": 0, "full_name": "a/b"},
        {"id": 777, "full_name": "a/other"},
        {"id": 777},
    ],
)
def test_numeric_alias_needs_authoritative_selected_repository_identity(metadata):
    transport = Transport(header("repositories/777/issues?" + QUERY + "&page=2"))
    transport.responses[ROOT] = ApiResponse(200, metadata, {})
    with pytest.raises(GithubError, match="repository identity"):
        GithubGateway(transport, "a/b").issues()
    assert transport.calls == [FIRST, ROOT]


@pytest.mark.parametrize(
    "link",
    [
        'https://api.github.com/repos/a/b/issues?page=2; rel="next"',
        "<https://api.github.com/repos/a/b/issues?page=2>; rel=next",
        header(ROOT + "/issues?" + QUERY + "&page=2")
        + ", "
        + header(ROOT + "/issues?" + QUERY + "&page=3"),
    ],
)
def test_malformed_or_ambiguous_link_does_not_report_partial_completion(link):
    transport = Transport(link)
    with pytest.raises(GithubError):
        GithubGateway(transport, "a/b").issues()
    assert transport.calls == [FIRST]


def test_alias_and_query_reordering_cannot_hide_a_cycle():
    query = "per_page=100&direction=asc&sort=updated&state=open"
    transport = Transport(header("repositories/777/issues?" + query))
    with pytest.raises(GithubError, match="cyclic"):
        GithubGateway(transport, "a/b").issues()
    assert transport.calls == [FIRST, ROOT]


def test_repository_identity_read_failure_is_visible():
    transport = Transport(header("repositories/777/issues?" + QUERY + "&page=2"))
    transport.responses[ROOT] = ApiResponse(503, {"message": "unavailable"}, {})
    with pytest.raises(GithubError, match="HTTP 503"):
        GithubGateway(transport, "a/b").issues()
    assert transport.calls == [FIRST, ROOT]


def test_numeric_pagination_limit_does_not_return_partial_inventory():
    second = FIRST + "&page=2"
    transport = Transport(header("repositories/777/issues?" + QUERY + "&page=2"))
    transport.responses[second] = ApiResponse(
        200, [{"id": 2}], {"link": header("repositories/777/issues?" + QUERY + "&page=3")}
    )
    with pytest.raises(GithubError, match="incomplete"):
        GithubGateway(transport, "a/b", max_pages=2).issues()
    assert transport.calls == [FIRST, ROOT, second]
