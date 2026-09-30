# Command line

Installing `pytest-httpchain` also installs a `pytest-httpchain` command. None
of it runs your tests or makes an HTTP request — the commands read scenario
files and report on them, and [`import`](#import) writes one from traffic you
recorded. None of them runs your code either, with one exception:
`validate --deep`, which imports the modules your `module:func` references name,
and therefore executes their top-level code; that is why it is opt-in and never
runs at pytest collection time. Every command reads a file as collection does,
[comments and trailing commas](usage/scenarios.md#comments-and-trailing-commas)
included, whether it is named `.json` or `.jsonc`.

If you only want to try one, `uvx pytest-httpchain --help` needs no install:

```bash
uvx pytest-httpchain --help
uvx pytest-httpchain --version
```

## Common options

Two options appear on every command that reads a scenario file, because both
affect how `$include`/`$merge`/`$ref` resolve:

| Option | Default | Meaning |
| --- | --- | --- |
| `--root-path DIR` | pytest's `rootdir` | Directory references must not escape. Collection uses pytest's `rootdir`, and so does the CLI by default: the one [pytest would determine](https://docs.pytest.org/en/stable/reference/customize.html#initialization-determining-rootdir-and-configfile) for a run on the same paths from the current directory (`validate a b` holds both to the rootdir of `pytest a b`). Pass it when a run uses `--rootdir` or `-c`. |
| `--ref-parent-traversal-depth N` | `3` | How many `../` levels a reference may climb. Mirrors the `httpchain_ref_parent_traversal_depth` ini option. |

The default root is the directory of the configuration file pytest would read
(`pytest.toml`, `pytest.ini`, or a `pyproject.toml` with a `[tool.pytest…]`
table, ...), else of the nearest `setup.py`, else the common ancestor of the
current directory and the paths, as pytest's is. A sub-package's own
`pyproject.toml` without pytest configuration does not shrink the root below
pytest's, and the files of one run share one root, so `validate` rejects no
reference that collection resolves. A file that root does not hold, as when one
run names the files of two projects (a pre-commit hook in a monorepo), is held
to the root pytest gives it alone, its own project's. A configuration file that
cannot be read, or that pytest would refuse, is passed over here as one without
pytest configuration is; pytest itself would stop on it.

## `validate`

Check scenario files and exit non-zero if any is invalid. This is the CI gate.
Name the files, or the directories that hold them:

```bash
pytest-httpchain validate tests/
pytest-httpchain validate tests/test_login.http.json tests/test_orders.http.jsonc
```

A file you name is validated whatever its name. One named anything but `.json`
or `.jsonc` gets an `HTTPCHAIN013` warning: pytest would not collect it.

Findings carry a stable `HTTPCHAINxxx` code and a severity — see
[Validation diagnostics](diagnostics.md) for the full table.

| Option | Meaning |
| --- | --- |
| `--format text\|json` | `json` emits the whole result, including each diagnostic's `code`, `severity`, `message` and `location`, for editor and CI integration. |
| `--strict` | Treat warnings as failures for the exit code. |
| `--suffix SUFFIX` | Search directories for `test_<name>.<SUFFIX>.json` and `.jsonc` files. Default: the `httpchain_suffix` your pytest configuration sets, else `http` (see [Directories](#directories)). |
| `--deep` | Also import your `module:func` references and check their signatures, and confirm referenced files and schema files exist. A body schema is followed as the runtime follows it: its JSON pointer must resolve, the schema it selects must be valid, and every `$ref` and `$dynamicRef` it reaches must resolve to a valid schema, locally and under the path rules a scenario's `$include` keeps (`--root-path`, `--ref-parent-traversal-depth`) (see [JSON Schema validation](usage/responses.md#a-schema-inside-a-document-openapi-and-shared-schema-files)). |
| `--syspath DIR` | Extra directory on `sys.path` for `--deep` import resolution. Repeatable. |

`--deep` imports your code, which is why it is opt-in and never runs at pytest
collection time:

```bash
pytest-httpchain validate --deep --strict --syspath tests tests/
```

In the JSON payload, the top-level `valid` is the gate result — it matches the
exit code and accounts for `--strict` — while each file's own `result.valid` is
pure validity. The sibling `strict` key tells the two apart. `files` holds an
entry per path reported, in the text report's order: each file checked, and
each directory without scenario files, with its `HTTPCHAIN039`.

The same semantic checks run at pytest collection time, so `pytest
--collect-only` validates your whole suite: error-severity findings fail
collection, warnings become `ScenarioValidationWarning`. A file that fails to
load reports the identical diagnostic code either way.

### Directories

A directory is searched the way pytest collects it, so `validate tests/` checks
the files `pytest tests/` runs:

- every file named `test_<name>.<suffix>.json` or `test_<name>.<suffix>.jsonc`,
  at any depth;
- except in the directories pytest skips by default: those its default
  `norecursedirs` matches (`*.egg`, `.*`, `_darcs`, `build`, `CVS`, `dist`,
  `node_modules`, `venv`, `{arch}`), `__pycache__`, and virtual environments (a
  directory holding a `pyvenv.cfg`, or a `conda-meta/history` for a conda
  environment). A directory you name is searched whatever its name. Your
  project's own `norecursedirs`, `--ignore` and `collect_ignore` are not read;
- passing over an entry pytest passes over because it cannot be looked at, such
  as a symlink to itself or a file deleted during the search; any other such
  failure (a permission error) stops `validate` with an `error:` line, as it
  stops pytest;
- not entering a symlink to a directory (or a Windows junction), so a link
  cannot send the search round in a loop. This is where `validate` differs from
  pytest, which follows one. A symlink to a file is checked like the file, and a
  directory you name may be a symlink.

The suffix is the `httpchain_suffix` set in the configuration file pytest would
read for the same paths (`pytest.toml`, `pytest.ini`, `pyproject.toml`,
`tox.ini` or `setup.cfg`, found as pytest
[finds its configfile](https://docs.pytest.org/en/stable/reference/customize.html#initialization-determining-rootdir-and-configfile)),
else `http`. `--suffix` overrides it, as `-o httpchain_suffix=...` overrides it
for pytest (`validate` reads neither `-o` nor `PYTEST_ADDOPTS`). A configuration
file pytest could not read, or a suffix it would refuse, stops `validate` with
an `error:` line before anything is checked. Files you name need no suffix, so
that happens only when a path is a directory.

The report is sorted by path, whatever order the paths are given in, so it does
not change with the order a shell or `find` lists them in. Paths are compared
name by name, which puts a directory's files in pytest's order: depth first,
each directory's entries by name. A file reached twice, as `validate tests
tests/api` reaches `tests/api`'s, is checked once, under the path it was first
reached by. After more than one file, a line sums the run up, counting a file
with errors under errors only:

```console
$ pytest-httpchain validate tests/
tests/api/test_orders.http.jsonc: OK with warnings
  warning [HTTPCHAIN003]: Stage 'list': request references potentially undefined variable(s): ['order_id'] (at stages[0].request)
tests/api/test_users.http.json: INVALID
  error [HTTPCHAIN001]: Duplicate stage names found: ['get'] (at stages)
tests/test_login.http.json: OK
3 files checked, 1 with errors, 1 with warnings
```

A directory holding no scenario file fails the run with `HTTPCHAIN039`, and a
path that does not exist with `HTTPCHAIN010`, so a mistyped path, or a suffix
that names no file, cannot pass CI as an empty run. Neither is a file checked:
the summary counts them apart, `..., 1 path not found, 1 directory without
scenario files`.

## `show`

Summarize a scenario: its stages, what each one saves, and where each consumed
variable comes from.

```console
$ pytest-httpchain show tests/integration/examples/save/test_save_jmespath.http.json
test_save_jmespath.http.json
2 stage(s) · fixtures: server

1 · save_jmespath    GET {{ server }}/users
    saves:    first_user, first_user_name, user_count
2 · use_saved_values    GET {{ server }}/user/{{ first_user.id }}
    saves:    fetched_name
    consumes: first_user (from #1 save_jmespath), first_user_name (from #1 save_jmespath), user_count (from #1 save_jmespath)
```

The first line is the scenario's `description`, or the file name when it has
none. A stage also lists its `marks:` when it declares any, and its `skip_if:`
when it may [skip](usage/scenarios.md#skipping-a-stage-at-runtime). The chain
goes on past a skipped stage, and a later stage then reads what an earlier
stage saved under the same name, or finds nothing. So a name whose nearest
producer has a `skip_if` lists every stage it may come from, nearest first,
back to one without a `skip_if`: `token (from #2 refresh, else #1 login)`.

`--format json` emits the same data-flow model (stages, edges, scenario
fixtures and vars) for tooling. Each stage carries its `skip_if` as declared,
`false` when it has none.

## `graph`

Render the stage data-flow as a [Mermaid](https://mermaid.js.org) flowchart.
Edges are labelled with the variables that create the dependency. An edge out
of a stage with a `skip_if` is dotted (`-.->`). When that stage skips, the
consumer reads those names from an earlier stage that saved them, so every such
stage has its own edge too, back to the nearest one without a `skip_if`, whose
edge is solid; when none saved them, the consumer finds nothing.

```console
$ pytest-httpchain graph tests/integration/examples/save/test_save_jmespath.http.json
flowchart TD
    S0["1 · save_jmespath"]
    S1["2 · use_saved_values"]
    S0 -->|first_user, first_user_name, user_count| S1
```

`--direction` takes `TD` (top-down, the default) or `LR` (left-to-right). The
output is Mermaid source; paste it into any renderer that accepts it, including
GitHub Markdown.

## `resolve`

Print the scenario with every `$include`/`$merge`/`$ref` inlined and
deep-merged — what collection actually sees. Useful when a merge is not
producing what you expected. The output is strict JSON: the
[comments and trailing commas](usage/scenarios.md#comments-and-trailing-commas)
a scenario or an included file may hold are not in it, so it is also a way to
turn a `.jsonc` scenario into plain JSON.

```bash
pytest-httpchain resolve tests/test_login.http.json
```

Inline JSON Schemas under `verify.body.schema` pass through verbatim, exactly as
they do at collection time: their `$ref` and `$defs` address the schema
validator, not the scenario resolver.

## `schema`

Emit the JSON Schema for scenario files, matching your installed version.

```bash
pytest-httpchain schema > scenario.schema.json
```

Point your editor at it with a `$schema` key in each test file for as-you-type
validation and autocomplete. Published copies are also served per release — see
[Getting Started](getting-started.md).

## `import`

Write a starter scenario from traffic you already have: a HAR file a browser's
developer tools (or the plugin's own [HAR export](#from-a-har-file)) saved, or
curl commands as an API's docs, a browser's "Copy as cURL" or a failing stage's
report give them. It sends nothing: each recorded request becomes a stage, in
order, and the scenario is yours to edit from there, to save the token one
stage returns for the next, say.

```bash
pytest-httpchain import har session.har -o tests/test_checkout.http.json
pytest-httpchain import curl -o tests/test_orders.http.json 'curl https://api.example.com/v1/orders -H "Accept: application/json"'
```

The scenario goes to stdout, or with `-o`/`--output` to a file, which must not
exist unless `--force` is given. Before it is written, the file it makes is
validated as [`validate`](#validate) validates one, read from disk the same way:
a scenario that would not pass (a URL or a method the model refuses) is not
written, and the command exits 1 with the findings; a warning is printed and the
scenario still written. Everything but
the scenario goes to stderr, so `> file` works too: a warning names what was
not imported, and the notes list what was left out and the placeholders to
fill in.

### From a curl command

`import curl` takes the command as one argument, as pasted (quoted for your
shell), as its words, already split by your shell, or `-` to read it from
stdin. Give `-o` and `--force` first: everything from the first word that is
neither on is the command's (its name `curl`, a URL, or a curl option such as
`-sSL`), a curl `-o` included, which a warning names (it is curl's file for
the answer, not the scenario's); an argument holding a whole command followed
by more is refused, saying so. The text is read with POSIX shell (bash)
quoting: single and double quotes, `$'...'`, a `\` before a line break (or a
CRLF), `#` comments. A `$VAR` is not expanded, with a warning. Text holding
several commands, one per line or after `;` or `&&`, each with `curl`, makes
a stage of each. A line that is not one (`which curl`, `apt-get install -y
curl`: a `curl` with nothing after it names no curl command) is refused by its
number, as is a curl command that sends nothing, such as one without a URL.

The command is the one named `curl`. What comes before that name, as docs and
terminals show it, is left out with a warning: a prompt's `$`, `sudo -E`, a
`NAME=value` for curl's environment, `watch -n 1`. A root shell's prompt `#`
reads as a comment, so text with no other command but a comment that is a curl
command is read as that command, with a warning. Without its name, a command
must start with an option or a URL with its scheme (`-X POST https://...`),
and be the text's only one: anything else, such as a `wget` or HTTPie command,
is refused rather than its words taken for URLs.

In a pipeline, the other commands are left out with a warning (`| jq .`), and
so is a redirection of curl's output (`> out.json`, `2>&1`). What `@-` reads
is curl's standard input where the text says what it is: a here-document
(`<<'EOF'` and the lines up to `EOF`), a here-string (`<<< 'a=1'`), a file
(`< body.json`, or a plain `cat body.json |` before curl, read as `@body.json`
would be), or what a plain `echo '...' |` or `cat <<'EOF' |` writes. Any other
command piped into curl is named in a warning, and what `@-` would read from it
left out.

```bash
pytest-httpchain import curl - <<'END'
curl -X POST https://api.example.com/v1/orders -H 'Content-Type: application/json' -d @- <<'EOF'
{"sku": "A-1", "quantity": 2}
EOF
END
```

```bash
pytest-httpchain import curl -o tests/test_orders.http.json - <<'END'
curl -X POST 'https://api.example.com/v1/orders?dry_run=true' \
  -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.x' \
  -H 'Content-Type: application/json' \
  -d '{"sku": "A-1", "quantity": 2}'
END
```

```console
note: secrets were left out of the scenario, which reads them from these environment variables:
  API_TOKEN  the bearer token (stage post_v1_orders)
```

```json
{
    "description": "Imported from a curl command",
    "substitutions": [{"vars": {"api_token": "{{ env('API_TOKEN') }}"}}],
    "client": {"base_url": "https://api.example.com", "follow_redirects": false},
    "stages": [
        {
            "name": "post_v1_orders",
            "request": {
                "method": "POST",
                "url": "/v1/orders",
                "params": {"dry_run": "true"},
                "auth": {"bearer": "{{ api_token }}"},
                "body": {"json": {"sku": "A-1", "quantity": 2}}
            },
            "response": [{"verify": {"status": "2xx"}}]
        }
    ]
}
```

The options that shape the request are mapped as curl would send it:

| curl | Scenario |
| --- | --- |
| URL (or `--url`), `-X`/`--request`, `-I`/`--head`, `-G`/`--get` | `url` and `method`, as curl picks it: `POST` with a body, `GET` with `-G`, which moves the data to the query. A word curl takes for a URL whose host has no dot or port (and is not `localhost`) is named in a warning: it is most likely no URL |
| URL globbing, `-g`/`--globoff` | a stage per URL curl makes of one with sets and ranges (`{a,b}`, `[1-3]`, `[01-10:2]`, `[a-z]`), with a warning; a URL curl refuses (`{}`, `[x]`) or one making more than 100 requests is refused. `-g` sends the URL as written, and so do an IPv6 address's brackets and `[]` |
| `-H`/`--header` | `headers`; `Name;` sends it empty, and `Name:` removes one curl would send itself, such as its default `Content-Type` |
| `-d`/`--data`, `--data-ascii`, `--data-raw`, `--data-binary`, `--data-urlencode`, `--json` | the body, joined and encoded as curl does, with the type curl gives it: `application/x-www-form-urlencoded` unless `-H` says otherwise, `application/json` (and an `Accept` of it) for `--json`. The file `--data-binary @file` and `--json @file` send as it is is a `binary` body, read when the stage runs. From `-d @file` and `--data-ascii @file` curl strips the line breaks (and NUL bytes), so the import reads that file now, from the directory it runs in, and writes its content as any other data; one it cannot read is a `binary` body, sent as it is, with a warning. `@-` reads standard input, where the text gives it (see above) |
| `-F`/`--form`, `--form-string` | a `multipart` body: `name=value` a field, `name=@path` a file (the path as written, with its `;type=` and `;filename=`), `name=<path` a part holding the file's content |
| `--url-query` | `params` |
| `-u`/`--user` (`--basic`, `--digest`), `--oauth2-bearer` | `auth` (see [Secrets](#secrets)) |
| `-A`/`--user-agent`, `-e`/`--referer`, `-b`/`--cookie` | the `User-Agent`, `Referer` and `Cookie` headers; `-A ''`, which removes curl's `User-Agent`, writes none (httpx sends its own); a `Cookie` header given with `-H` replaces `-b`'s cookies, as curl sends it, with a warning |
| `-L`/`--location` | redirects followed; without it, as with curl, they are not (`client.follow_redirects: false`, and `allow_redirects: true` on the requests that had `-L`) |
| `-k`/`--insecure` | `ssl.verify: false`, which is the scenario's: for every request, with a note when only some had `-k` |
| `-m`/`--max-time` | `timeout`, the client's when every request has the same; `-m 0`, curl's no limit, which a scenario has no setting for, is ignored with a warning (the client's 30 seconds apply) |

Options that change only curl's own output or connection are ignored without a
word: `-s`, `-S`, `-v`, `-i`, `-f`, `-o`, `-O`, `-w`, `-D`, `--trace`,
`--compressed` (httpx asks for and decodes compression on its own), and
the HTTP version options (the client negotiates HTTP/2 itself, see
[`client.http2`](usage/scenarios.md#client-configuration)). Any other option is
ignored with a warning naming it, whether curl has it (`--retry`, `-x`,
`--cacert`: its value is skipped as curl reads it; `--path-as-is`, since
httpx collapses a path's `/../`) or not; so is what a
scenario cannot hold, such as data read from a standard input the text does
not give (`-d @-` after `jq ... |`), a form part read from standard input
(`-F f=@-`), a cookie file (`-b jar.txt`) or a `--data-urlencode name@file`. A curl command sends a
request but records no answer, so each stage verifies a `2xx` status.

The curl command a [failing stage's report](troubleshooting.md#sending-the-failing-request-again) shows imports
back into the request it stands for: the same method, URL, headers and body.
That is a quick way to turn a failure into a scenario of its own. Its
`[REDACTED]` values become placeholders like any secret, and a binary body the
report's note saved as `body.bin` is read from that file.

### From a HAR file

`import har` makes a stage of each entry, in the file's order, verifying the
status its response had. `-` reads the file from stdin. A HAR records each
redirect as an entry of its own, so the scenario follows none
(`client.follow_redirects: false`). Some entries are left out, and a note says
how many:

- the static assets a page loads to render itself: images, stylesheets, fonts
  and scripts, known by their response's MIME type (or, where a browser
  recorded none, as for a `304`, by the resource type it recorded; Chrome
  writes such a MIME type as `x-unknown`), unless the
  page's own code fetched them (an XHR or `fetch()` request is kept, an image
  included). `--all` keeps them;
- with `--include REGEX`, the entries whose URL no such pattern matches, and
  with `--exclude REGEX`, those one matches (both repeatable, matched with
  `re.search`): `--include '/api/' --exclude '/api/telemetry'`;
- an entry that is not http(s) (`data:`, `ws:`), and one without a response
  (status 0: aborted or blocked), which has no status to verify.

The HAR the plugin writes with
[`--httpchain-output-dir`](getting-started.md) imports too, its bodies whole,
bytes included (with `httpchain_har_redact`, its hidden values become
placeholders).

The scenario's client keeps the cookies its responses set and sends them, as
the browser did, so a cookie a request sent because an earlier imported
response set it (the session a login answered with) is not written: the next
run's login sets its own. A request that also sent cookies no response set
(the browser had them before the recording) cannot send both kinds, since a
`Cookie` header of the scenario's own keeps the client from sending its
cookies: it sends the client's, and a note names the others. A multipart file
whose content the browser did not record is sent empty, with a note.

### What a scenario gets

- **URLs.** When every request goes to one origin, it is `client.base_url`,
  and each URL is relative to it; otherwise the URLs stay absolute. The query
  string is `params`, in its order, a repeated name's values a list. A query
  `params` would not send as it was stays in the URL, as written: one whose
  escapes are not UTF-8, one repeating a name apart from its first (`params`
  sends `a=1&b=2&a=3` as `a=1&a=3&b=2`), one with a parameter named
  `$ref`, `$include` or `$merge` (see [Reference keys](#reference-keys)), and
  one holding a [secret](#secrets), which `params` would send empty were its
  variable unset. A fragment is not sent, and is left out.
- **Headers** are kept as recorded, except those the client writes itself or
  that belong to one connection: `Host` (unless it names another host than the
  URL, for a virtual host), `Content-Length`, `Connection`, `Keep-Alive`,
  `Proxy-Connection`, `Transfer-Encoding`, `TE`, `Trailer`, `Upgrade`,
  `Accept-Encoding` (httpx sends its own and decodes the answer) and HTTP/2's
  pseudo-headers (`:authority`, `:path`, ...). `Proxy-Authorization` is left
  out with a note: a proxy's credentials go in
  [`client.proxy`](usage/scenarios.md#client-configuration)'s URL. A header
  sent twice is one, its values joined with `, `. With several requests, a
  header every one of them sends with the same value is the client's
  (`client.headers`), `Content-Type` excepted.
- **Bodies** take the form their `Content-Type` names: JSON
  (`application/json`, `+json`) a `json` body, a form
  (`application/x-www-form-urlencoded`) a `form` body, `multipart/form-data` a
  `multipart` body (its text fields, and its files by path, by recorded
  content, or as `base64` for bytes that are not text), and anything else, or
  what does not parse as its type says (JSON sent with curl's default form
  type, a number too large for a float), the raw `text`. So is a form a `form`
  body would not send as it was (a name repeated apart from its first), a form
  holding a [secret](#secrets), and a JSON document or form with a member
  named as a [reference key](#reference-keys).
  The `Content-Type` header is left out where the body
  form sets that very type itself (exactly `application/json`, the form type,
  a multipart boundary) and kept otherwise. Bytes a HAR recorded as base64 are
  a `base64` body, but for a multipart one, taken apart as a text one is (its
  secret fields placeholders), a curl `--data-binary @file` a `binary` one. The
  note lists the files the scenario reads, a relative path from the scenario's
  own directory. A JSON body
  nested over 100 levels deep is text too.
- **Authentication.** `Authorization: Basic` and curl's `-u` are the
  [`basic`](usage/requests.md#authentication) shorthand (`digest` with curl's
  `--digest`), `Authorization: Bearer` and `--oauth2-bearer` the `bearer` one,
  and they are the scenario's `auth` when every request sends the same. An
  `Authorization` header of another scheme is kept, its value a placeholder.
- **Stage names** are the method and the path's words: `post_v1_orders`, and
  `get_root` for `/`. The same request again is `get_users_2`.
- **Literal braces.** A `{{` in anything recorded is
  [escaped](usage/substitutions.md#literal-braces), so it is sent as recorded,
  not read as a template. In text a placeholder follows on its line (a query
  kept in the URL, a body kept as text), a `{{` that no `}}` follows is
  written `{{ '{{' }}`, a template rendering to the braces, since an escape
  would run on to the placeholder's own `}}`.

### Reference keys

A scenario file reads a key named `$ref`, `$include` or `$merge` as a
[reference](advanced/ref-merging.md) wherever it is,
so none that was recorded is written as a key: the loader would resolve it,
and the scenario would fail `validate` or send something else. A JSON
Schema or OpenAPI document posted to a registry, a MongoDB DBRef, an OData
`?$ref=` all have one. Instead, a JSON or form body holding one is the `text`
it was (with a note), its `Content-Type` kept; a query holding one stays in
the URL; a header named `$ref` is written `$Ref` (a header name's case says
nothing); and a multipart part of that name, which no form can hold, is left
out with a note.

### Secrets

No credential is written into the scenario. These values become placeholders,
each a `vars` entry the scenario reads from an environment variable, the way
the docs pass a secret (see [Authentication](usage/requests.md#authentication)):

- a Bearer token (`api_token`), a password (`api_password`), and a user name
  given without a password (`api_user`, as curl's `-u key:` or a URL's
  `https://<key>@host` gives an API key, sent with an empty password), which
  is the credential then;
- the cookies, one placeholder for the whole `Cookie` header (`cookie`);
- an `Authorization` header of another scheme (`authorization`);
- the headers and query parameters a failing stage's report
  [redacts](getting-started.md#secrets-in-reports) by default (`X-API-Key`,
  `access_token`, `password`, `token`, ...), named after them (`x_api_key`),
  and the form fields, multipart parts (a text field, or a file's recorded
  text, or its bytes, base64-encoded as recorded) and JSON members named as those query
  parameters are, at any depth of a JSON body: a string or a number there (a
  number is read with `json_loads`, to be sent as the number it was), each of
  a list's under that name. An object under the name is judged by its own
  members, since a JSON Schema's `"password": {"type": "string"}` holds none;
- a header whose value is a URL (`Referer`, `Location`, `Content-Location`)
  when the report would redact a credential in that URL, such as the page a
  browser's request came from, `/callback?access_token=...`: the whole header
  (`referer`).

The same value under the same name is one placeholder. The notes list each
environment variable, what it stands for and the stages using it; set them
before running the scenario (`API_TOKEN=... pytest tests/test_orders.http.json`).
One left unset renders as `null`. Where it is a whole value, that fails the
stage naming the field: `auth`, a header, the cookies, a multipart part. A
query parameter or form field would be sent empty instead (`params` and a
`form` body send a `null` as nothing), so a query or form holding a secret is
kept as the text it was, the query in the URL and the form a `text` body, the
secret a placeholder inside it, URL-encoded as it renders:
`/search?page=2&access_token={{ quote(access_token) }}`, which an unset
variable fails (`quote() takes text or bytes, not None`). A JSON member is
sent as `null`, as a `json` body sends a `null`; in a JSON body written as
`text`, it is rendered as JSON, `"password": {{ json_dumps(password) }}`,
which sends an unset one as `null` too. A number is read with `json_loads`,
which an unset one fails. Anything else a body holds is copied as recorded (a
plain text body, one nested too deep to walk): read it over before you commit
the scenario.
