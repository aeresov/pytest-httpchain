# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.1] - 2026-09-30

### Fixed

- A verify expression that renders to something other than a boolean no longer prints the value
  in its failure: `{{ response.headers['Set-Cookie'] }}` put the session cookie in the pytest
  report, past the redaction that hides it among the headers. The failure names the type alone,
  as `skip_if`'s does: `Verify expression 0 must evaluate to bool, got str, a value written where
  a condition belongs`.
- `pytest-httpchain import` no longer writes a secret multipart part recorded as bytes into the
  scenario. A part named as a redacted query parameter (`password`, `token`, ...) whose content
  is not text, as a HAR records it, was written as its `base64` value where a text part became
  a placeholder. It is a placeholder too now, which holds the part base64-encoded, as recorded.
- A `save.jmespath` expression that Python cannot evaluate against a response, such as
  `contains(s, n)` looking for a number in a string or `ceil(x)` of a number too large for a
  float, fails the stage as a save error, which `retry.on: save` retries, instead of escaping as a
  raw `TypeError` or `OverflowError` traceback. It reads as a `verify.jmespath` entry's does, and so
  does a save whose function is given the wrong type: `Error saving variable n: length() needs
  string or array or object, got 5 (number)`, where it quoted jmespath's message.
- `NaN`, `Infinity` and `-Infinity` in a scenario, a file it `$include`s, `$merge`s or `$ref`s, or
  a body schema file are syntax errors (`HTTPCHAIN014` at load, `HTTPCHAIN021` for a schema file
  under `validate --deep`), at their line and column: `NaN is not valid JSON: line 2 column 14`.
  Python's JSON parser reads them as numbers though JSON has none, so such a file passed
  `validate`, and `resolve`, documented to print strict JSON, printed them.
- `validate`, `resolve`, `show` and `graph` hold references to the rootdir pytest would determine
  for a run on the same paths, which collection holds them to, so `validate` no longer rejects
  with `HTTPCHAIN012` a reference that collection resolves. The CLI's root was an approximation
  of its own, which missed two cases: a `pytest.toml` or `.pytest.toml` configuration file, or a
  `.pytest.ini`, was not seen, so a sub-package's plain `pyproject.toml` below it set a narrower
  root; and each file of a `validate a b` run got a root of its own, the file's directory when
  nothing marked a project, where `pytest a b` has one, their common ancestor. Heads-up: without a
  pytest configuration file or a `setup.py`, the root is now what pytest's is, the common ancestor
  of the current directory and the paths (the paths' own when that is the root of the file system,
  or on Windows when they are on another drive than the current directory), in place of the
  nearest directory holding a `.git`, a bare project file or named `tests`; pass `--root-path` for
  another.

## [0.16.0] - 2026-09-29

### Added

- `HTTPCHAIN031` (error): an `xdist_group` in a stage's `marks` naming a group the scenario does
  not declare. xdist joins every group on a test into one name, so under `--dist loadgroup` that
  stage got a group of its own and ran on another worker, without the earlier stages' saved values.
  The mark belongs in the scenario's `marks`; a stage repeating the scenario's group is accepted.
- `HTTPCHAIN032` (error): a stage name containing `::` (`Users::list`). The name is part of the
  stage's node id, where `::` separates the parts, so pytest could not run the stage by its node
  id, and `--dist loadscope`, which groups tests by node id up to the last `::`, ran it apart from
  the rest of the scenario.
- `HTTPCHAIN033` (error): a scenario `xdist_group` name with a `]` after its last `@`, such as
  `xdist_group('db[main]')`. xdist ignores such a group, so under `--dist loadgroup` every stage was
  scheduled on its own, and a later stage could fail on another worker; `validate` reported the
  scenario as OK.
- Credentials are redacted in failure reports. A failing stage printed `authorization: Bearer <token>`,
  its cookies and any `?access_token=...` verbatim in the HTTP Request/Response sections, so they
  landed in CI logs. The values of the headers listed in the new `httpchain_redact_headers` ini
  option (default `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`,
  `API-Key`, `X-Auth-Token`) and of the query parameters in `httpchain_redact_query_params`
  (default `access_token`, `refresh_token`, `id_token`, `api_key`, `apikey`, `client_secret`,
  `password`, `token`) now print as `[REDACTED]`, names kept: `cookie: a=[REDACTED]; b=[REDACTED]`,
  `set-cookie: sid=[REDACTED]; Path=/; HttpOnly`. Names match case-insensitively, a list replaces
  its default, and an empty value disables it. The rules also cover a redirect's `Location`, a
  URL's userinfo (the password of `https://user:password@host`, the user name of
  `https://<token>@host`), the value a failed header check echoes (`Header 'Set-Cookie' doesn't
  match: expected sid=[REDACTED]; Path=/, got ...`, and a `not_contains` operand found in the
  hidden part), a request error quoting a header value (`Illegal header value b'[REDACTED]'` for
  a token with a trailing newline) and the `HTTP Request: GET <url>` line httpx logs at INFO.
  Bodies and DEBUG logs are not redacted. HAR files stay complete by default, as a HAR is usually
  replayed; the new `httpchain_har_redact = true` applies the same rules to their URLs, headers,
  cookies and query strings.
- A scenario-level `client` block configures the HTTP client all its stages share:
  `{"client": {"base_url": "https://api.example.com/v1", "headers": {"Accept": "application/json"}}}`.
  With `base_url`, a stage's `request.url` may be relative (`/users/1`), and httpx appends it to
  the base URL's path: `https://api.example.com/v1` and `/users/1` give
  `https://api.example.com/v1/users/1` (a leading `/` does not return to the host's root, as it
  would in a browser), an absolute stage URL ignores `base_url`, and both are still sent as
  written. `headers` go with every request, a stage's `request.headers` overriding them by name,
  case-insensitively; a `Content-Type` among them labels every request body except a `form` or
  `files` one, which keeps the type it is encoded in (`application/x-www-form-urlencoded`, or
  `multipart/form-data` with the boundary between its parts). `params` are added to every request's
  query unless the stage already sets the key, in its URL or its `params`, and like those they are
  merged into the URL's query without re-encoding it. `timeout` and `follow_redirects` are the
  defaults for the stages that do not set `request.timeout` or `request.allow_redirects` (30
  seconds and `true` when neither does): a stage's own value wins, including one written equal to
  the default or brought in by `$include`/`$merge`. `max_redirects`, `proxy` (which replaces the
  proxy environment variables), `http2` (default `true`), `max_connections` and
  `max_keepalive_connections` (default 20) map onto httpx's settings. An `https://` proxy's own
  TLS connection follows the scenario's `ssl` as the servers' do once `ssl` sets `verify: false`, a
  CA bundle or a `cert` (given the URL alone, httpx checks the proxy against certifi's bundle
  whatever `ssl` says); with the default `ssl` it is checked as httpx checks a proxy from
  `HTTPS_PROXY`, against the system's CA store and certifi's bundle. Templates in `client` resolve
  once per scenario, against the scenario substitutions only, like `ssl` and `auth`
  (`HTTPCHAIN016`/`HTTPCHAIN017` cover them), and one rendering to `null` on `base_url`, `proxy` or
  a pool limit fails initialization rather than dropping the setting, and so does one rendering
  to text that is still a template (`{{ ... }}`), which httpx would otherwise take as it is. A
  `client` value that fails validation is reported without the value, which may be a credential
  (a proxy URL's password, a header's token). `base_url` may not have a query or fragment (httpx
  would append the path after them); put default query parameters in `params`. A URL without a
  scheme is now a relative URL, so `not-a-url`, which failed schema validation, fails as
  `HTTPCHAIN034` unless the scenario sets `base_url`.
- `HTTPCHAIN034` (error): a stage `request.url` that is relative, all of it or the text before
  its first template (`/users/{{ id }}`), while the scenario's `client` sets no `base_url`. A
  template that renders to a relative URL without one fails its stage with the same message,
  before anything is sent, instead of httpx's `Request URL is missing an 'http://' or 'https://'
  protocol`.
- Built-in authentication, without a Python function: `{"basic": {"username": "...", "password":
  "..."}}`, `{"digest": {"username": "...", "password": "..."}}` and `{"bearer": "<token>"}`, in the
  scenario's `auth` or a request's. The values take templates: a scenario's resolve once, against
  the scenario substitutions (`HTTPCHAIN016`/`HTTPCHAIN017` cover them, as they cover `ssl` and
  `client`), and a request's with the rest of the request, so `{"bearer": "{{ token }}"}` sends the
  token a login stage saved, and `validate` reports a token used before the stage that saves it
  (`HTTPCHAIN004`); `show` and `graph` draw the edge. A credential whose template renders to
  `null` fails the stage rather than sending the request without credentials, a bearer token may
  not be empty, and a credential that fails validation is not quoted. The whole `auth` may be one
  template (`"{{ creds }}"` over a `vars` object), at either level; a user function name a
  template in `auth` rendered is not quoted when it fails to import either, since credentials
  rendered as one `"user:password"` string have a name's shape. The scenario's digest auth
  answers the server's first challenge and then every later request up front, parallel ones
  included, counting each use of the nonce once as servers checking for replays require; a
  request's is challenged on each request, and its challenge shows in the HAR export as a request
  without credentials, as it went out (httpx added the answer to the challenged request). A
  request's `"auth": false` sends it without the scenario's auth, for a public endpoint in an
  authenticated scenario; at scenario level `false` fails validation, saying where it belongs.
  User functions (`"module:function"` or `{"name": ..., "kwargs": ...}`) work as before, for any
  other scheme; an object mixing `name` with a built-in's key, or two built-ins, fails validation
  naming the extra key, and `validate --deep` imports only user functions.
- `verify.status` takes a status class and a list besides one code. `"2xx"` passes any 200-299
  response (the classes are `"1xx"` to `"5xx"`, in either case), and a non-empty list such as
  `[200, 201]` or `["2xx", 304]` passes when the response matches any of its entries, each a code
  (100-599, as before) or a class. A mismatch names what was expected the way it was written:
  `expected 2xx, got 500`, `expected one of [200, 201], got 500`, and `expected 200, got 500` as
  before for one code. A `status` template may render to any of these forms, and a list entry may
  be a template of its own (`["{{ created_status }}", 409]`), which `validate` checks and
  `show`/`graph` count as consuming the names it reads, like any other. A whole `status` that
  renders to `null` still fails its stage naming the template; a list entry that does, an empty
  list or a value that is neither a code nor a class fails it too, with the validation report on
  the rendered value. A template that rendered to text which is itself a template, which failed
  as `expected {{ y }}, got 200`, now fails as `verify.status must resolve to a status code or a
  class such as 2xx, got '{{ y }}'`, even beside an entry that matched. A `$merge`/`$include`
  sibling's `status` list is not concatenated onto a fragment's, as other lists are: a longer list
  of alternatives accepts more, so a negative test's `[404]` beside a shared `["2xx"]` would have
  passed on a 200. An equal list is kept and a different one is a merge conflict, as a differing
  single code always was.
- `verify.jmespath` asserts on the JSON response body directly, where a value had to be saved first
  and then tested in an expression, leaving the saved name in the context. Each key is a JMESPath
  expression, mapped to the value it must equal or to a matcher: `{"data.id": "{{ user_id }}",
  "length(items)": 3, "price": {"gt": 0, "lt": 100}, "meta": {"eq": {"page":
  1}}}`. Equality is JSON's: `true` never equals `1`, `1` equals `1.0`, and arrays and objects
  compare element by element. An object written in the scenario is always a matcher, with the keys
  `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `contains`, `not_contains`, `matches`, `not_matches`, `type`
  and `length`, every one given must hold, and a value one cannot judge (`gt` on a string) fails
  the check; a literal object written for equality fails validation pointing at `eq`, beside the
  matcher's own errors when its keys are all a matcher's (`{"type": "admin"}`). A template written
  where a value goes renders the value to compare with, an object included, whatever its keys:
  `"meta": "{{ saved_meta }}"` compares by equality, and a matcher is written as an object, its
  operands templated. A failure names the expression, the expected value and the actual one, cut
  short when long: `JMESPath 'price' doesn't match: expected lt 100, got
  120.5`. The checks run after `headers` and before `expressions`, the order of a verify step's
  checks is now documented, and the body is parsed once per step, shared with `body.schema`; one
  that is not JSON, or is nested too deeply to parse, fails the stage, and so does an expression
  that cannot be evaluated against it, naming why (`keys() needs object, got [1, 2] (array)`,
  `join() needs array-string, got an array holding 1 (number)`, `ceil()` of `1e400`). Keys are
  never rendered: one holding a template fails validation, or gets `HTTPCHAIN029` when it is still
  valid JMESPath (`'{{ x }}'`). Values and operands are rendered, in the response step's scope,
  which `validate` checks and `show`/`graph` count as consuming what they read. A value template
  rendering to `null` compares with `null`; an operand template rendering to `null` fails the stage
  like other rendered-away checks, `eq` and `ne` included, since there `null` would be compared in
  place of the lost value: the message says to write `null` for that. A missing path gives `null`,
  as JMESPath has it; `contains` on the parent object tells a missing key from a null one.
  `HTTPCHAIN006` counts `jmespath` as an assertion, and `HTTPCHAIN007`/`HTTPCHAIN008` flag a
  matcher whose `contains` and `not_contains` are the same JSON value, or whose `matches` and
  `not_matches` are the same pattern. A `$merge`/`$include` sibling's expectation for an expression
  a fragment already checks merges whole, as `status` does: equal keeps, anything else is a merge
  conflict. A reference written at the expectation itself, `{"$merge": "common.json#/price", "lt":
  100}`, composes one matcher key by key, and an operand both sides give must agree whole: two `eq`
  arrays or objects conflict rather than concatenate or blend, as two `gt` numbers do.
- A failing stage's report has an `HTTP Request (curl)` section: the request as a curl command, to
  send it again from a shell. It is shown whenever the `HTTP Request` section is, for the same
  request and under the same label (`(after 1 redirect)`, `(failing of 3 parallel iterations)`).
  Every argument is single-quoted for a POSIX shell, so quotes, newlines, shell syntax and non-ASCII
  text in a header or body reach curl as sent. The headers curl writes itself (`Host`,
  `Content-Length`, `Transfer-Encoding`, `Connection`) are left out, except a `Host` the request
  set itself and the `Content-Length: 0` of a bodyless `POST`, which curl would not send; the
  `Accept-Encoding` httpx sends on its own becomes `--compressed`, and one the request set
  (`identity`) is kept, with `--compressed` to decode what comes back as httpx does; an empty
  header is sent empty (`-H 'X-Empty;'`), and a body without a `Content-Type` is not labelled a
  form, as curl would. A textual body is given as sent (`--data-raw`); a binary one, or one over
  10,000 characters, is read from a file a comment above the command names (`--data-binary
  @body.bin`), and a multipart body that was not captured, a stream the plugin does not send
  itself, gets a comment saying to add its parts with `-F`. The report's redaction applies: a
  hidden value stays `[REDACTED]`, and a comment says to fill it in. A digest `Authorization`,
  which answered one challenge and cannot answer another, is left out, and a comment says to add
  `--digest -u 'user:password'`. A URL holding brackets or braces gets `--globoff`, so curl does
  not read them as ranges. The scenario's `ssl` and `client.proxy` settings are not part of the
  request, and the command has none.
- `save.regex` saves values from a body that is not JSON, such as a CSRF token in an HTML form or an
  id in plain text: `{"regex": {"csrf": "name=\"csrf\" value=\"([^\"]+)\"", "order_id": {"pattern":
  "Order #(?P<id>\\d+)", "group": "id"}, "all_ids": {"pattern": "id=(\\d+)", "all": true}}}`. A
  pattern is searched for in the body's text (`re.search`), and the variable is group 1 of the first
  match when the pattern has groups, else the whole match. As an object, `group` picks a group by
  number (`0` for the whole match) or by name, and `all` saves a list of that group from every
  match, `[]` when none. A pattern that does not match fails the step, naming the variable and the
  pattern; a group that took no part in its match saves `null`. Patterns may hold templates, so a
  `{{` in one always opens a template and literal braces are escaped (`\\{\\{`), and `group` and
  `all` may be templates. An invalid pattern, and a group a written-out pattern does not have, fail
  `validate` and collection; once a template renders them, they fail the stage as a save error, as
  does template text a template renders there. The saved names are known to `validate`'s order
  checks and to `show`/`graph`, as a JMESPath save's are.
- Template built-ins for the values a test otherwise needed a fixture or a user function for.
  Time: `now()` is the current UTC time in ISO 8601 with its offset and always with microseconds
  (`2026-09-27T12:34:56.789012+00:00`), `now('%Y-%m-%d')` formats it with `strftime` (except `%s`,
  which the C library formats as local time: `timestamp()` is the epoch value), and `timestamp()`
  and `timestamp_ms()` are Unix seconds and milliseconds. Encoding: `b64encode` and `b64decode`
  (text as UTF-8, `urlsafe=true` or a second argument `true` for the URL-safe alphabet, padding
  optional when decoding, so a JWT segment reads as it is), `json_dumps` (`json.dumps`' defaults,
  a `vars` object written as the object it is) and `json_loads`. URLs: `urlencode` builds a query
  string from an object as `params` sends one (a list repeats its key, `true`/`false`, an empty
  value for `null`, bytes percent-encoded as they are rather than as their `b'...'` repr), and
  `quote` percent-encodes a path segment, `/` included unless given in `safe`. Hashing: `sha256`,
  `md5` and `hmac_sha256(key, message, encoding='hex')`, hex or `'base64'`, for signing a request.
  Each returns plain text, a number or JSON data; a value it cannot take (a number to hash, text
  that is not base64 or not JSON, an object or an uncalled function nested in `urlencode`) fails
  the stage with a message naming the function, and so does a template that renders to a helper
  uncalled (`{{ now }}`, or a save named `now` that has not landed), rather than sending
  `<function now at 0x...>`. A name the scenario defines itself, a variable, fixture, parameter,
  save or function substitution, still wins over a built-in of the same name where it is in scope,
  `get()` and `exists()` excepted: `{{ timestamp }}` reads a saved `timestamp`, while
  `{{ timestamp() }}` calls the built-in unless that `timestamp` is a fixture's or function
  substitution's function. The docs' three "use a fixture for a timestamp" examples now use
  `now()`.
- `HTTPCHAIN035` (warning): a built-in function that is no use as a value, a helper above or
  `uuid4`, `env`, `rand` or `randint`, used without calling it, such as `{{ now }}`, `{{ env }}`,
  `str(timestamp)` or `dict(at=timestamp)`, which gets the function itself rather than its value. A
  built-in handed to a function that may take one, a `key=`, a user function's argument or the
  argument of a method of a fixture's object (`helper.ids(uuid4)`), is not reported, nor is one in
  text the runtime never renders (a function substitution's `kwargs`). In the scenario-level
  `substitutions` of a scenario whose parametrize values are templates, the warning says that the
  scenario's collection fails, which resolves them there, not its initialization.
- `HTTPCHAIN036` (warning): a built-in's name the scenario defines too, used as a function where
  that definition is not in scope, such as `timestamp()` in another stage or in the scenario-level
  `substitutions` when one stage fakes the clock with a `timestamp` function substitution, or
  `sorted(rows, key=len)` ahead of a save named `len`. The built-in runs in its place, which is
  never a failure, so it is a warning at every level, scenario level included. Handed to your own
  function, the name counts only where your definition may hold a function, a fixture or function
  substitution: `sign(timestamp)` ahead of a save named `timestamp` gives `sign` the built-in
  function, not the saved text, and is reported as any read ahead of its save.
- `HTTPCHAIN037` (warning): a template in a stage that the engine refuses from its text alone, with
  the reason the stage fails with: a syntax error, more than one statement, an assignment or
  another statement, a kind of expression the engine does not evaluate (a lambda, a set
  comprehension, `*` unpacking outside a list literal, `yield`, `await`), an attribute it does not
  read (`doc._id`, `'{}'.format(x)`) or a call of anything but a name or an attribute. The stages
  before it still run, so it is a warning, as an undefined name there is (`HTTPCHAIN003`).
- `HTTPCHAIN038` (error): the same in a template resolved before any stage runs, where it leaves
  nothing to run: a scenario-level one (`substitutions`, `auth`, `ssl`, `client`), which fails
  scenario initialization and every stage with it, as an undefined name there does
  (`HTTPCHAIN017`), or a stage's parametrize value, which fails the scenario's collection, as do
  the scenario's `substitutions` once a templated parametrize value resolves them there. It fails
  collection and `validate`.
- `verify.body.schema` checks a response against a schema inside a document you already have, an
  OpenAPI 3.1 component or a file of shared definitions:
  `"schema": "./openapi.json#/components/schemas/User"`. What follows the file's first `#` is an RFC
  6901 JSON pointer written as a URI fragment (percent-decoded, `~1` for a `/` in a key, `~0` for a
  `~`), so a path's response schema is
  `openapi.json#/paths/~1users~1{id}/get/responses/200/content/application~1json/schema`. The schema
  it selects keeps its `$ref`s to the rest of the document (`#/components/schemas/Address`), and a
  `$ref` to another local file (`./address.json`, `common.json#/$defs/Email`) resolves relative to
  the file it is written in; an inline schema's resolve relative to the scenario file's directory.
  The dialect is the selected schema's own `$schema`, else the document root's, else Draft 2020-12,
  and a schema a `$ref` reaches follows the same rule, so a definition in a Draft 7 file is Draft 7
  however it is reached; `format` is checked in every schema a reference reaches. An `$id` in the
  schema is the base of the references inside it, as JSON Schema specifies, at its root, on the
  pointer's way, and in an OpenAPI component too, where JSON Schema itself does not look for one,
  whether the pointer selects the component or a schema inside it: a bundled document's
  `"$ref": "/schemas/address"` under `"$id": "https://example.com/schemas/customer"` finds its
  embedded `/schemas/address` resource. A document a `$ref` reaches is resolved against where it is,
  whatever `$id` its root declares, as jsonschema resolves one. A reference that names a local file
  keeps the rules a scenario's `$include` path keeps: a relative path, at most
  `httpchain_ref_parent_traversal_depth` `../`, to a file inside pytest's rootdir. Nothing is read
  over the network: a remote reference is not fetched (see Changed), and a file on another host (a
  UNC path on Windows) is refused before its path is looked up. Each file is parsed, meta-checked
  and its `$id`s and anchors indexed once while it is unchanged, however many stages, parallel
  iterations (which wait for the one reading it) and references use it, and each reference is looked
  up once per verify step, not once per array item it validates. A missing or unreadable file, a
  pointer that leads nowhere (`'#/components/schemas' has no key 'Usr'`), an `$id` on its way that
  cannot be read (`$id 5 is not a string`), a selected schema that is not valid, a `$ref` that does
  not resolve and a schema a `$ref` reaches that validating against shows to be invalid
  (`$ref '#/components/schemas/Role' points to an invalid JSON Schema: ...`, as `validate --deep`
  words it) each fail the stage as one verification error naming the file and the pointer. An
  `http(s)` URL in place of the file, or a fragment that is not a pointer (`openapi.json#User`),
  fails `validate` and collection. `validate --deep` checks the rest without a response: that the
  file exists (`HTTPCHAIN020`), is JSON, that the pointer resolves, that the schema it selects is
  valid, and that every `$ref` and `$dynamicRef` it reaches resolves under the same rules to a
  schema valid in its dialect (`HTTPCHAIN021`, or `HTTPCHAIN020` for a local file that is not
  there), following what the runtime follows: a reference beside a Draft 3 to 7 `$ref`, which they
  ignore, is not reached. An OpenAPI 3.0 document's schemas are a dialect of their own (`nullable`,
  boolean `exclusiveMinimum`); the docs say how to convert them.
- A `multipart` request body sends form fields and files together, which `files` could not:
  `{"multipart": {"fields": {"title": "Report", "tags": ["a", "b"], "draft": false}, "files":
  {"document": "./report.pdf"}}}`. A field is text, a number or a boolean, sent as text the way a
  form value is (`false` as `false`), and a list sends a field per item under the same name. Each
  `files` entry is one file or a list of them, a part per file under the same name. A file is a
  path, as before, or an object with exactly one of `path`, `content` (text, sent UTF-8 encoded)
  and `base64` (binary data), and optionally `filename` (by default the path's last component, or
  for inline content the field's name; `""` sends none, for a JSON part beside a file) and
  `content_type` (by default guessed from the filename's extension, else
  `application/octet-stream`). The fields go first, then the files, in the order written.
  `files` takes the same file objects and lists; a path string is what it was. Templates work in
  every value, a whole file object or list of files included, and a `filename` or `content_type`
  template that renders to `null` fails the stage, `which would silently send the default filename
  instead`, as other optional fields do; a `null` in a source a rendered file object does not use
  is not set, as in the object written out. A file that is not there fails the stage naming its
  path as the scenario gives it, tidied as any path is (`./report.pdf` as `File not found for
  upload: report.pdf`), and `validate --deep` reports it (`HTTPCHAIN020`), a file object's `path`
  and a list's items too. A stage's own `Content-Type` is sent as written, and the parts are
  delimited by the boundary it names, a `multipart/related` or `multipart/mixed` one included,
  where httpx only took the boundary of a `multipart/form-data` one and delimited the parts with
  another. A multipart type naming no boundary, such as `multipart/form-data` written out of
  habit, is sent with the body's added, where the server had none to find the parts by; one
  naming an empty boundary fails the stage, as does one naming a boundary the parts cannot be
  delimited by as written (holding a `;`, ending in whitespace, or starting or ending with a quote,
  none of which RFC 2046 allows), where `boundary="a;b"` had the parts delimited by `a`.
- A stage-level `skip_if` skips a stage when a condition holds, decided when the stage is about to
  run, where marks are fixed at collection and `always_run` only counts after a failure:
  `"skip_if": "{{ target == 'prod' }}"`, or `"skip_if": true`. A template sees what the stage's
  request sees but the `parallel.foreach` parameters: fixtures, parametrize parameters, scenario
  substitutions, earlier stages' saves and the stage's own `substitutions`, which run first. It
  must evaluate to a boolean, as a verify expression must: a saved string `"false"`, a number or
  a `null` fails the stage (`skip_if must evaluate to bool, got str from '{{ flag }}'`, the type
  and never the value, which may be a token) rather than skip it or run it by truthiness. The
  stage is reported skipped as `skip_if: <the template>`, sends nothing and saves nothing, and the
  chain goes on: a skip is no failure. In a chain a failure aborted, a stage skips as before unless
  `always_run` lets it through, and then its `skip_if` still counts. A literal `true` skips without
  running the stage's substitutions. The validator checks a `skip_if`'s references like every
  other template's (`HTTPCHAIN003`/`HTTPCHAIN004`, `HTTPCHAIN035`). Since the stages after a
  skipped one run, it also reports a later stage reading a name that only stages with a `skip_if`
  save, other than through `get()`, as potentially undefined (`HTTPCHAIN003`): `request references
  'token', which only stage 'login' saves, and it has skip_if: when it skips, 'token' is undefined
  here`. The saves of a stage that may fail still count as there, since the stages after a failure
  do not run unless they are `always_run`. `show` lists a stage's `skip_if`, and `graph` draws the
  edges out of such a stage dotted. A name re-saved by a stage with a `skip_if` comes from the
  stage before that saved it when it skips, so both list every stage such a name may come from,
  back to one without a `skip_if`: `token (from #2 refresh, else #1 login)`.
- `parallel.collect_saves: true` keeps every iteration's saves. They merged into one value per
  name, the highest iteration index winning, so a stage creating several resources kept the id
  of one of them, and "create N, then clean them all up" could not be written. Now each name any
  iteration saves becomes a list with one entry per iteration, in iteration order whichever
  finished first (entry `i` lines up with iteration `i`'s `foreach` parameters), and `null` where
  an iteration did not save the name. A later stage goes through them with `"foreach":
  [{"individual": {"id": "{{ created_ids }}"}}]`, such as an `always_run` cleanup. It works with
  `repeat` and `foreach` alike, a single iteration's lists included, and inside the stage an
  iteration's later response steps still read the value it saved itself. Saves stay all or
  nothing: a stage with a failed iteration commits no list at all. It can be a template, rendered
  with the rest of the `parallel` config before any request is sent, against what the stage's
  `skip_if` sees: a `foreach` parameter there is undefined, and `validate` reports it
  (`HTTPCHAIN003`) as in any `parallel` setting. It must render to a boolean, the numbers `1` and
  `0` counting as `true` and `false` as in `client.http2`; any other value, `null` included, fails
  the stage before its iterations send anything. The default, `false`, merges as before, and the
  validator, `show` and `graph` count the names as the stage's saves either way.
- A stage's `retry` attempts it again while it fails, which polling an asynchronous job until it is
  done needs, as does a read that is only eventually consistent: `"retry": {"attempts": 10, "delay":
  0.5, "backoff": 2, "max_delay": 5, "on": ["verify", "save", "request"]}`. `attempts` counts the
  first; `delay` (default 1 second) is the wait before the second attempt, multiplied by `backoff`
  (default 1) after each, never more than `max_delay`. Each attempt renders the request anew, a
  fresh `uuid4()` included, sends it and runs every response step in a context of its own: a failed
  attempt's saves are discarded, and only the one that passes commits its saves. `on` (one kind or a
  list, default all three) names the failures retried: a verify step's checks, a save step that
  could not take its values from the response, and the request timing out or its connection failing.
  A verify or save function is retried when it returns false or raises a `VerificationError` or
  `SaveError` (`pytest_httpchain.errors`) to say "not yet". Never retried, as every attempt would
  fail alike: a template that cannot be rendered or renders a value its field or check does not
  take, a user function that cannot be imported or found or that crashed, a body schema that cannot
  be read (a missing file, a pointer that leads nowhere, an invalid schema, a `$ref` that does not
  resolve), a request httpx refuses to send, too many redirects, an auth function that raised, a
  rate-limit slot that did not come, and a user function's `pytest.skip()`/`xfail()`/`fail()`. After
  the last attempt the stage fails with that attempt's failure, `... (after 10 attempts)`, as does a
  `pytest.fail()` or a failure never retried on a later attempt, and the report shows that attempt,
  `HTTP Response (attempt 10 of 10)`; the HAR export records every attempt of every iteration, and
  each retried failure logs a line at `INFO`. A sibling `on` beside a `$merge` of a shared `retry`
  is a merge conflict, as a sibling `verify.status` list is, rather than concatenated: its kinds are
  alternatives, and concatenated they would retry more than either side wrote. In a `parallel` stage
  each iteration retries on its own, a wait ends at once when another iteration fails the stage, and
  each attempt takes a `calls_per_sec` slot, a retried single iteration's too. What factory fixtures
  enter during the attempts is exited when the iteration ends. The settings but `on` take templates,
  rendered once per stage before any request against what `skip_if` sees; `validate` checks their
  references like the `parallel` config's (`HTTPCHAIN003`/`HTTPCHAIN004`, `HTTPCHAIN035`), a setting
  the stage cannot use fails it before the first request, and a `max_delay` rendered to `null` is
  refused rather than lift the cap. A `delay`, `backoff` or `max_delay` that is not finite (JSON's
  `1e999`) is refused at load, by `validate` too, and so is a `true` or `false` in any of the four,
  written or rendered, which would have been read as `1`: `"attempts": "{{ poll }}"` with a flag for
  `poll` attempted once.
- Comments and trailing commas in scenario files. Every JSON file the plugin reads from disk, a
  scenario, a file it `$include`s, `$merge`s or `$ref`s, a `verify.body.schema` file and the files
  its `$ref`s reach, may hold `//` line comments, `/* */` block comments and one trailing comma
  before a `]` or `}`, at collection, at runtime and in `validate` (`--deep` too), `resolve`, `show`
  and `graph`. Strictly valid JSON reads as it did, so there is nothing to switch on. pytest also
  collects `test_<name>.<suffix>.jsonc`, the extension editors open as JSON with comments, and
  `validate` takes it without `HTTPCHAIN013`; a `.json` and a `.jsonc` of the same name are two
  scenarios, their node ids told apart by the extension. Nothing inside a string is touched, and a
  syntax error's line and column still point into the file as written, as comments are read as
  whitespace. A `/*` never closed is a syntax error at its opening, and a leading or doubled comma
  stays one: `HTTPCHAIN014` in a scenario or a file it includes, and in a `verify.body.schema` file
  a failed stage, which `validate --deep` reports ahead of time as `HTTPCHAIN021`. A response body
  stays strict JSON, and a file a request uploads is sent as it is. `resolve` prints strict JSON,
  the comments gone.
- `pytest-httpchain validate` takes directories as well as files, so a CI job can gate a whole
  tree with `pytest-httpchain validate tests/`. A directory is searched as pytest collects it:
  every `test_<name>.<suffix>.json` and `.jsonc` at any depth, passing over the directories pytest
  skips by default (those its default `norecursedirs` matches: `*.egg`, `.*`, `_darcs`, `build`,
  `CVS`, `dist`, `node_modules`, `venv`, `{arch}`; `__pycache__`; a virtual environment, known by
  its `pyvenv.cfg`, or a conda environment by its `conda-meta/history`) and the entries pytest
  passes over when they cannot be looked at (a symlink to itself), and never entering a symlink to
  a directory, so a link cannot loop the search. The suffix is the `httpchain_suffix` set in the
  configuration file pytest would read for the same paths (`pytest.toml`, `pytest.ini`,
  `pyproject.toml`, `tox.ini` or `setup.cfg`, found as pytest documents finding its configfile),
  else `http`. The new `--suffix` option overrides it, and a configuration file pytest could not
  read, or a suffix it would refuse, stops `validate` with an `error:` line before anything is
  checked; a run over files alone never reads it. Files named one by one are still validated
  whatever their name. The report is sorted by path, whatever order the paths are given in (a
  directory's files so in pytest's order, depth first, each directory's entries by name), a file
  reached twice (`validate tests tests/api`) is checked once, and `--deep`, `--syspath`,
  `--strict`, `--format json` and the reference options apply to every file found. A directory
  holding no scenario file is reported as `HTTPCHAIN039` (error) and fails the run, so a mistyped
  path, or a suffix that names no file, cannot pass CI as an empty run.
- Templates read a `vars` object by key as well as by attribute, at any depth and in lists, as they
  read an object a `save` took from a response: `{{ headers['Content-Type'] }}` for a key that is
  no Python name, `{{ doc['_id'] }}` for one starting with `_`, `{{ 'name' in user }}`,
  `{{ len(user) }}`, `{{ [k for k in user] }}` (the keys, in the order written), `user.keys()`,
  `user.values()`, `user.items()` and `{{ user.get('nick', 'anon') }}`. The `response` metadata of
  a response step reads by key too (`{{ response['status'] }}`). An attribute reads the object's
  key first, as it always has, so for an object with a key named `keys`, `values`, `items` or `get`,
  `{{ order.items }}` is still that key's value and `{{ order.items() }}` fails calling it, as do
  `dict(order)` and `{**order}` for a key named `keys`, which they call; every form by key
  (`order['items']`, `list(order)`, `[order[k] for k in order]`, `{k: order[k] for k in order}`)
  reads the data. For an object without such a key, the attribute reaches the method only to call
  it (`order.items()`) or to hand it as a `key=` (`{{ max(scores, key=scores.get) }}`); anywhere
  else in an expression (`{{ order.items != [] }}`, `bool(order.get)`, `str(order.keys)`) it is
  the missing attribute it was, so a check reading it still fails rather than pass on the method.
  (A saved object is a dict, whose methods an attribute reads before its keys: `saved.items` is the
  dict's method, so read a key named like one by key, `saved['items']`.) A missing key fails the
  stage naming it, a missing attribute named like a method says how to call it, and an empty object
  is now false (all under Changed). Nothing else changes: an object interpolated into text still
  reads `namespace(...)`, however deep, it still equals another `vars` object with the same keys
  and values and no dict, and a JSON body, `json_dumps` and `urlencode` take it as before.
  `validate` reads a subscript, `in` and the methods as it reads an attribute: the object's name is
  the one reference.
- A backslash escapes a template's braces: `\{{` in a value renders as the text `{{` and opens no
  template, so a Handlebars or Mustache payload, documentation text or a `{{placeholder}}` a server
  expects is sent as written (`"X-Template": "\\{{name}}"` in the JSON file sends
  `X-Template: {{name}}`). The escaped text runs to the first `}}` after it on its line, so
  template syntax in it is sent as text, only the escaping backslash dropped (a Handlebars raw
  block's `\{{{{raw}}}}`, Jinja's own `\{{ '{{' }}`, `\{{{{ id }}`, where `id` is not evaluated);
  after that `}}` text is read as usual, so each tag of a payload takes its own backslash, and a
  `}}` needs no escape.
  Before `{{`, `\\` stands for one backslash, so a backslash before a template is written doubled
  (`\\{{ id }}` renders a backslash, then the value), each pair before `{{` renders as one, and a
  backslash anywhere else is left as it is. The escape is removed once, when the scenario's own
  text renders: a value a template puts in, a save, a variable or a fixture's value, holding `{{`
  or `\{{` is put in as it is. Every value that renders takes the escape (request fields, verify
  operands and `jmespath` values, `save.regex` and `matches` patterns, `vars` and parametrize
  values, file paths: a `binary` body, an upload, an `ssl` file). `validate` agrees: an escaped
  template is no template, so no name in it is reported as undefined, a literal is checked as the
  text it renders to (a GraphQL query or a JMESPath expression holding `\{{` in one of its strings,
  whose own backslash escapes have none for `{`, is valid), `validate --deep` looks for the file
  a path with an escape renders to, a field that takes only a whole template (`timeout`,
  `skip_if`) refuses an escaped one as text, and the client's `base_url` and `proxy` and a
  `verify.body.schema` file reference, which take no literal braces, refuse a backslash before
  `{{` as the scenario loads. A key and a `functions` kwarg are never rendered, so they keep a
  backslash as written, and `validate` warns of an escape there (`HTTPCHAIN029`, `HTTPCHAIN030`):
  it does nothing, and the braces need none. The expression form `{{ '{{' }}` renders `{{` too, as
  it did. See [Literal braces](docs/usage/substitutions.md#literal-braces).
- A parallel stage measures its iterations and can be held to limits on the numbers: used as a small
  load test, it reported only whether it passed. Its report has a `Parallel Summary` section, shown
  where the `HTTP Request` and `HTTP Response` sections are (a failed stage's report, a passed one's
  with `-rP` or `-rA`, under pytest-xdist too): how many iterations passed, failed and were
  cancelled (and were skipped, when a user function skipped one), the success ratio, the wall time,
  the throughput (completed and passed iterations per second of wall time) and the passed
  iterations' latency, min, mean, p50, p95, p99 and max in milliseconds, the percentiles
  nearest-rank. An iteration's duration is the time its requests spent in the HTTP client,
  redirects, every attempt of a `retry` and the client's own wait for a pooled connection included,
  but not the wait for a `calls_per_sec` slot or between attempts, so that a rate limit does not
  read as a slow server. `parallel.thresholds` fails the stage once every iteration has ended below
  a `min_success_ratio` or a `min_rps`, or above a `max_mean_ms`, `max_p50_ms`, `max_p95_ms` or
  `max_p99_ms`, naming every limit missed with the value measured (`2 parallel thresholds not
  met: 1. min_success_ratio: 0.666667 (6 of 9 iterations passed), below the limit 0.9 ...`). A
  `min_success_ratio` below 1 lets the stage run on after failures: no iteration is cancelled, the
  stage fails at the end only if too few passed, listing the first five failed iterations, and it
  saves what the passed ones saved (with `collect_saves`, `null` in a failed iteration's place; at a
  ratio of 0 with none passed, `null` for each name its steps declare, which a later stage can still
  read). Without one, or at 1, the first failure cancels the rest and fails the stage as before, and
  a user function's `pytest.skip()`, `xfail()` or `fail()`, or a factory fixture's context manager
  raising on exit, still ends the stage at once, the exit error's failure listing the iterations
  that failed on their own; the HAR file has what every one of them sent, however the stage ends.
  `parallel.stats_as` saves the stats as an object (`iterations`, `passed`, `failed`,
  `success_ratio`, `wall_ms`, `rps`, `completed_rps`, `min_ms`, `mean_ms`, `p50_ms`, `p95_ms`,
  `p99_ms`, `max_ms`) for the stages after it to read (`{{ load.p95_ms }}`), only when the stage
  passes; `show` and `graph` list the name among the stage's saves, `validate` reports a reference
  to it from the stage's own steps (`HTTPCHAIN004`), and the new `HTTPCHAIN040` (warning) a name the
  stage's response saves too, which the stats replace. The thresholds are numbers or templates,
  rendered with the rest of the `parallel` config before any request: one that renders `null`, text,
  `true` or `false`, or a value out of its range fails the stage before its iterations send
  anything. See [Stats and thresholds](docs/advanced/parallel.md#stats-and-thresholds).
- `pytest-httpchain import har FILE` and `pytest-httpchain import curl COMMAND` write a starter
  scenario from traffic you already have: a browser's HAR export (or the plugin's own), or curl
  commands from an API's docs, a browser's "Copy as cURL" or a failing stage's report, whose command
  imports back into the request it stands for. Each request is a stage, in order, verifying the
  status the HAR recorded (a curl command records none: `2xx`). The origin every request shares is
  `client.base_url`, a query string `params` (or the URL's own, as written, where `params` would not
  send it as it was: a name repeated apart from its first, escapes that are not UTF-8, a secret
  `params` would send empty unset), a JSON, form or multipart body the `json`, `form` or
  `multipart` form (anything else the raw `text`, bytes `base64` but for a multipart body, taken
  apart as a text one is, a curl `--data-binary @file` `binary`; a recorded `$ref`, `$include` or `$merge` key,
  which the file's loader would resolve, never becomes a key of the scenario, so a JSON Schema
  posted to a registry is sent as the text it was), Basic and Bearer credentials the `basic` and
  `bearer` auth shorthands (the scenario's `auth` when every request sends the same), and with
  several requests a header they all send alike `client.headers`; transport headers (`Host`,
  `Content-Length`, `Connection`, `Accept-Encoding`, HTTP/2 pseudo-headers, ...) are left out, and a
  recorded `{{` is escaped, so it is sent as recorded. No secret is written: a token, a password,
  a user name sent without one (curl's `-u key:`, a URL's `https://<key>@host`), the cookies, the headers and query parameters reports redact (and form fields, multipart parts and
  JSON members, strings or numbers, named like those parameters), and a `Referer` or other
  URL-valued header whose URL carries such a parameter become placeholders the scenario reads from
  environment variables (`{{ env('API_TOKEN') }}`), which stderr lists; one left unset fails its
  stage, and inside text kept as written they render as it needs them (`{{ quote(access_token) }}`
  in a URL, which fails unset too). `import curl` takes the command as one argument, as its words or
  from stdin (`-`), read with POSIX shell quoting (`$'...'`, `\` line continuations and comments
  included; several commands make a stage each). The command is the one named `curl`: what comes
  before the name (a prompt's `$`, `sudo -E`, a `NAME=value`, `watch -n 1`) and the rest of a
  pipeline are left out with a warning, a command without the name must start with an option or a
  URL, and another program's command (`wget ...`) is refused rather than its words taken for URLs;
  what `-d @-` reads is the here-document, here-string, `< file` or plain `cat`/`echo` piped into
  curl the text gives. It maps curl's request options (`-X`, `-H`, the `-d` family, `--json`, `-F`,
  `-G`, `-u`, `-A`, `-e`, `-b`, `-L`, `-k`, `-m`, ...) as curl sends them (a `Cookie` header given
  with `-H` in place of `-b`'s cookies), curl's default form type,
  the line breaks it strips from a `-d @file`, its URL globbing (a stage per URL of `{a,b}` and
  `[1-3]`, unless `-g`) and its not following redirects without `-L` included; output options are
  ignored (`-o` with a warning, since it may have been meant as the import's), and any other option
  is named in a warning, never dropped silently. `import har` leaves out the static assets a page
  loaded (images, stylesheets, fonts and scripts, by MIME type or, for a `304` Chrome recorded as
  `x-unknown`, by resource type, unless the page's code fetched them; `--all` keeps them) and the
  entries `--include` and `--exclude` patterns filter out, follows no redirect (each is an entry),
  and leaves a cookie an earlier response set to the scenario's client, which keeps it as the
  browser did. The scenario is written to stdout or `-o`/`--output` (an existing file only with
  `--force`), and only once the file passes the validator, read back as `validate` reads it: one
  that would not, a URL or method the model refuses, fails the command with the findings; what
  cannot be read (a malformed URL, a file name holding a NUL) is an `error:` line and exit status 1.
  See [`import`](docs/cli.md#import).

### Fixed

- A JSON syntax error in a file a scenario `$include`s, `$merge`s or `$ref`s was reported by
  `validate` and at collection as `HTTPCHAIN014` with a line and column but no file name, which
  read as a position in the scenario itself; `HTTPCHAIN015` for such a file nested too deeply named
  no file either. Both now name the file: `Invalid JSON syntax in .../common.json: Expecting value:
  line 2 column 8 (char 9)`.
- The warnings that a scenario's earlier stages were deselected, or that a fixture's params vary
  across its stages, and the error that `--dist loadscope` would split it, named the scenario by
  its name alone, which scenarios of one name in different directories share, as do the
  `test_<name>.http.json` and `test_<name>.http.jsonc` now collected side by side. They now give
  its node id, `test_login.http.jsonc::login`.
- `{{ env }}`, the `env` built-in written without its parentheses, rendered the repr of
  `os.environ`'s `get`, which lists every environment variable with its value, into the request,
  and so into the HAR file and the report, and `validate` said nothing. A template that renders to
  `env`, `uuid4`, `rand` or `randint` uncalled now fails the stage as one that renders to a helper
  does (`Uncalled function in expression '{{ env }}': ... call it: env(...)`), `HTTPCHAIN035`
  warns of it, and `env` inside an expression (`str(env)`) no longer carries the environment in its
  text.
- A name the scenario defines that a template built-in also has, such as a save called `max`,
  `sum` or `round` (or, now, `timestamp` or `now`), was left out of `validate`'s checks and of
  `show`/`graph`: every built-in's name was dropped from a template's references, so a later stage
  reading the save drew no edge, and a read before the save was not reported, though it rendered
  the built-in function (`<built-in function max>`) into the request. A name the scenario defines
  is now a reference wherever a template reads it, which is what the runtime resolves: out of scope
  it is reported, with a note that the built-in is used instead. A call to a fixture or function
  substitution named like one, made where that fixture or function is not in scope, silently ran
  the built-in, and still does; `HTTPCHAIN036` now says so, as a warning at scenario level too,
  where such a call does not fail.
- A header matcher's `matches` or `not_matches` that a template rendered to text `re` cannot
  compile, such as `{{ ( }}` saved from a response (the field's template branch takes template
  text as it is), escaped the stage as a raw `re.error` traceback, and one too big to compile
  (`a{4294967296}`, or thousands of nested groups) as an `OverflowError` or `RecursionError`. It
  now fails its check: `Header 'X-Request-Id' (value: '12345'): matches must resolve to a regular
  expression, got '{{ ( }}' (missing ), unterminated subpattern at position 3)`.
- A response body Python's `json` cannot read though it is not malformed, an integer longer than
  4300 digits or an array nested some thousand levels deep, escaped `verify.body.schema` and a
  JMESPath `save` as a raw `ValueError` or `RecursionError` traceback, past the chain's abort
  handling, with no request/response report and no HAR entry. Both now fail the stage: `Cannot
  ..., response is not valid JSON: ...`, or `... response JSON is nested too deeply to parse:
  ...`. A `verify.body.schema` file nested that deeply fails with "Error reading body schema
  file", and `validate --deep` reports it as `HTTPCHAIN021`. The HTTP report shows such a body as
  text; it used to show an error placeholder that also dropped the start line and headers.
- `verify.body.schema` no longer escapes as a bare `RecursionError` on a body or schema file that
  parses but is some hundreds of levels deep. A violation found in such a body is reported
  without the value pretty-printed, which recursed past Python's limit, and a schema that recurses
  as deep as the body (`"items": {"$ref": "#"}`) fails the check with `Cannot validate schema,
  response or schema is nested too deeply`. `validate --deep` no longer crashes while describing
  why such a schema file is invalid, and reports `HTTPCHAIN021`.
- A `$merge`/`$include` sibling beside a fragment at a position that merges whole, an inline
  `verify.body.schema` or a `verify.status` list, was compared with Python's equality inside
  lists and objects, where `true == 1`: `{"const": true}` beside `{"const": 1}` kept one and
  dropped the other without a conflict. Equality there is now JSON's at every depth, as it was
  for a single value.
- A failing stage whose response came after a digest challenge (an auth function returning
  `httpx.DigestAuth`, or the new built-in) was reported as `HTTP Request (after 1 redirect)`: httpx
  keeps the challenge's `401` in the same history as redirects. The report now counts them apart,
  `(after 1 auth exchange)`, or `(after 1 redirect and 1 auth exchange)` for both.
- A request that failed validation once its templates rendered printed the refused value in
  pydantic's report (`input_value=...`), such as a header's token that rendered to a number; the
  failure now leaves the values out, as a `client` block's does.
- A scenario `auth` written as one template (`"{{ creds }}"`) was validated, once rendered, as
  the user function name it was declared as: one rendering a `{"name": ..., "kwargs": ...}`
  object failed initialization with pydantic's report, which printed the rendered object, while
  `validate` passed the file. A request's `auth` rendering a `vars` object failed its stage the
  same way. Both now take any form of auth, and the scenario's failure leaves the value out.
- An `auth` written as one template that rendered a string other than a `module:function` name,
  typically a token (`"auth": "{{ token }}"` for `{"bearer": "{{ token }}"}`), failed with two
  messages that each quoted it, putting the credential in the report. The failure is now one
  message, at either level, that says what a string `auth` is and where a token goes without
  quoting the string.
- A `combinations` step whose combinations have a single key now hands the stage the bare value.
  The stage got a one-element tuple instead: with `{"combinations": [{"id": 1}, {"id": 2}]}`,
  `/item/{{ id }}` requested `/item/(1,)`, a stage that did not check the value still passed, and
  `validate` reported nothing. Both a literal list and a template resolving to one were affected;
  `parallel.foreach` was not. Such a step now behaves exactly like `individual`, generated test
  ids included (`[1]` instead of `[id0]`). Because the ids now come from the values, a
  template that draws them at random (`uuid4()`, `rand()`, `randint()`) gives every
  pytest-xdist worker different ids, and the run stops with "Different tests were collected";
  give such a step explicit `ids`, as `individual` and multi-key `combinations` already needed.
- A template that renders to `null` no longer silently switches off an optional check or setting.
  Only `verify.status` and `verify.body.schema` were guarded. A header matcher field such as
  `{"Content-Type": {"contains": "{{ expected_ct }}", "not_contains": "text/html"}}`, with
  `expected_ct` from a JMESPath save of a missing key, simply went unchecked and the stage passed;
  `parallel.calls_per_sec: "{{ get('rate') }}"` without a `rate` ran the stage with no rate limit;
  a stage-level `auth` sent the request unauthenticated, or with the scenario-level credentials
  when the scenario had its own `auth`; `ssl.cert` connected without the client certificate.
  Every model the engine renders is now compared with its declared form, and an optional field
  whose template rendered to `null` fails the stage (for `ssl`, scenario initialization) naming
  the field and the template as written:
  `'verify.headers.Content-Type.contains' was declared as '{{ expected_ct }}' but rendered to None, which would silently disable it`.
  Optional fields added later are covered too, and so is a matcher written as one template
  (`"Content-Type": "{{ matcher }}"`, with `matcher` saved from the response): a key it sets to
  `null` fails the same way, while a key it leaves out is simply not checked. The
  `status`/`body.schema` failure now reads the same way, and so does a field where validation
  rejects `null` anyway, with the message ending at "rendered to None": a matcher whose only
  field rendered to `null` failed with "Header matcher must set at least one of: contains, ...",
  asking for the field the scenario had set, and a required field such as `url` with pydantic's
  type errors ("URL input should be a string or URL"); neither named the template. When
  something else in the same model is invalid as well, pydantic's report on it follows the
  message.
- A JSON body of `null` is sent as the JSON document `null`. `{"json": null}`, or a `json` template
  that rendered to `null`, went out as an empty request with no `Content-Type`, exactly like a
  request without a body. It is now `null` with `Content-Type: application/json`, unless the
  request sets its own content type.
- `params` no longer throws away a query string already in `url`. With
  `"url": "{{ server }}/items?page=2"` and `"params": {"limit": 10}` the request went to
  `/items?limit=10`: httpx replaces the URL's query with `params` instead of adding to it, and
  nothing reported the lost `page=2`. The two are now merged: the URL's parameters come first,
  then the new keys from `params`, and a key present in both takes its value from `params`
  (`/items?page=2&limit=10`). A list value still repeats the key. Every URL parameter that
  `params` does not set goes out exactly as it would without `params`, order and encoding
  included, so an escape that is not UTF-8 (`q=%E9`) or a bare `?flag` reaches the server
  unchanged. The HTTP report section and the HAR export show the merged URL, as sent.
- A request URL reaches httpx as written. It was validated as pydantic's `HttpUrl`, and the
  WHATWG-normalized form that type hands back was what got sent: `{{ server }}/static/%2e%2e/ok`
  went out as `/ok`, so a path-traversal probe hit a different endpoint and passed or failed for
  the wrong reason; a `\` in the path went out as `/`; and a URL longer than 2083 characters was
  refused, a limit httpx does not have. The URL is still checked, at collection for a literal
  one and after rendering for a templated one, to be an absolute `http`/`https` URL with a
  well-formed host and port, with the same messages as before, but the string itself is what
  httpx now gets. These, which WHATWG accepted only by repairing them, are refused instead,
  since they would now be sent unrepaired or to another host and port than the ones checked:
  `http:/example.com`, a leading or trailing space, a control character anywhere (a tab or a
  line break was dropped, `\x01` percent-encoded), a `\` before the path (`http://a\b/` went to
  host `a`), a percent-encoded host (`http://ex%61mple.com/`), a host only a browser's IDNA
  mapping accepts (fullwidth letters or a soft hyphen, folded to plain ASCII). Other Unicode
  whitespace at either end, such as U+3000 or U+00A0, is part of the URL to both and is sent.
  A literal URL with an empty `{{ }}` in it, which was sent with the braces percent-encoded, is
  refused at collection like any other empty template. httpx itself still resolves a literal
  `..` segment, as curl does; write it as `%2e%2e` to send it. A URL may now be up to 65,536
  characters, httpx's own limit, and the editor schema drops the 2083-character `maxLength`.
- A scenario that lists a `class`-scoped (or broader) fixture with `params` in its `fixtures` runs
  its whole chain once per param. It ran every param's first stage before any second one: with a
  `tenant` fixture over `[a, b]`, `create[a]`, `create[b]`, `read[a]`, `read[b]`. `read[a]` saw
  what `create[b]` saved and failed, `read[b]` was skipped, and the fixture was set up four times,
  once per test, instead of once per param. pytest runs such tests param by param (`create[a]`,
  `read[a]`, `create[b]`, `read[b]`); the plugin's sorting of each scenario into stage order undid
  that. Each param's stages now form a chain of their own, in stage order, run one chain after the
  other, so the fixture is set up once per param. Each chain also starts like a new run of the
  scenario: nothing the previous chain saved, no abort from its failure, and its own HTTP client,
  where before only the end of the scenario reset them. The scenario's `substitutions`, `auth` and
  `ssl` still resolve once for all its chains, so a user function there is not called again per
  param. A `-k`, `--lf` or `--deselect` selection that keeps a later stage of one param's chain
  but drops an earlier one draws the usual warning, naming the chain (`the chain for
  tenant='a'`). A fixture with `params` that is function-scoped, or that only some stages
  request, still varies in place like a stage's `parametrize`, within one chain, whichever of
  those stages you run, with `-k` or by node id. When two or more stages, but not all, request
  such a `class`-scoped (or broader) fixture, collection now warns: each of those stages runs for
  every param before the next one does, so `read[a]` sees what `create[b]` saved, and the fixture
  is set up again at each change of param. Requesting it from every stage, e.g. in the scenario's
  `fixtures`, runs the chain once per param instead.
- pytest-xdist `--dist loadgroup` and `--dist loadscope`, the modes documented as keeping a
  scenario together, could still run one of its stages on another worker, where it failed without
  the earlier stages' saved values. loadscope groups tests by node id up to the last `::`, and
  loadgroup by their `xdist_group` names, which xdist appends to the node id after an `@`. Besides
  the scenarios the new `HTTPCHAIN031` to `HTTPCHAIN033` reject (see Added), two more split a chain.
  Under loadgroup, a scenario in a directory such as `[smoke]`: the automatic group is named after
  the scenario's node id, and xdist ignores a group whose name has a `]` with no `@` after it, so
  every stage was scheduled alone. The plugin now replaces `]` and `@` in that name. Under
  loadscope, a test id containing `::`, from a parametrize step's `ids` or values (such as
  `"::1"`) or from a class-scoped fixture's `params`. pytest itself handles such an id, so it
  fails collection only under loadscope, naming the ids.
- An `xdist_group` in a scenario's own `marks` now works as it does for any pytest test: scenarios
  declaring the same group run on one worker under `--dist loadgroup`, one after the other, where
  they used to run side by side. The plugin added its automatic group on top, and xdist joined the
  two into a name of each scenario's own (`db_test_orders.http.json`). The plugin now adds its
  group only to a scenario that declares none; every stage inherits the declared one, so the chain
  still stays together.
- A `parallel.foreach` `combinations` step written as one template over scenario `vars`, such as
  `{"combinations": "{{ combos }}"}`, now runs the stage once per combination. It failed the stage
  with pydantic's "Input should be a valid dictionary" report, while the same template worked in
  stage `parametrize`: `vars` makes each object attribute-accessible, and only `parametrize` took
  such an object for the combination it stands for. The model now does that for both, in a list
  or any other sequence the template renders (`{{ tuple(combos) }}`), and one level deep, so an
  object nested inside a combination keeps its attribute access (`{{ owner.name }}`).
- A `verify.body.schema` or a header matcher written as one template over scenario `vars`, such
  as `"schema": "{{ user_schema }}"` or `"Content-Type": "{{ ct }}"` with `ct` set to
  `{"contains": "json"}`, now checks the response. Both failed the stage with pydantic's "Input
  should be a valid dictionary" report, while the same template over a value saved from a response
  worked: neither field took a `vars` object for the object it stands for. Now both do, a schema
  down to every object nested in it.
- A stage `parametrize` step whose template resolves to another template string now fails
  collection with a message naming the step and the stage. Both step kinds also accept a template,
  so the text passed re-validation: an `individual` step then ran one test per character, and a
  `combinations` step failed collection with a pydantic error for each character. 0.15.2 closed
  the same gap for `parallel.foreach`.
- A context manager returned by a factory fixture (`{{ transaction() }}`) is exited when the stage
  that entered it ends, while the fixtures it is built on are still there. It was exited only
  once the whole scenario was done, after pytest had torn down the stage's fixtures and even the
  `class`-scoped ones: a transaction on a `class`-scoped `connection` fixture was committed on a
  connection already closed. An exception raised on exit was only logged, and the stage and the
  run stayed green. The exit now happens at the end of the stage, whether it passed, failed or was
  skipped, last entered first. One entered by the request or a response step is exited as soon as
  the response steps are done, in the thread that ran them: in a `parallel` stage, each iteration
  exits its own in its worker thread, so a context manager tied to its thread (a `sqlite3`
  connection) works, and an iteration's transaction does not stay open while the others run. An
  exception raised on exit fails the stage (in a `parallel` stage, the iteration, which cancels
  the others), even one a user function skipped or xfailed, with a message naming the fixture:
  `Exiting the context manager from fixture 'transaction' failed: RuntimeError: ...`. If the stage
  had already failed, its own failure message comes first. A failing iteration of a `parallel`
  stage cancels the others before its own exits, so a slow one (a rollback) does not let the
  queued iterations send their requests meanwhile. An iteration still running when another one
  fails or skips a `parallel` stage exits its own when it ends all the same. In a `parallel`
  stage, what an iteration's exits raise is labelled with the iteration
  (`Iteration 1: Exiting the context manager ...`), apart from the stage's own. Like any failure,
  it discards the stage's saves and aborts the chain. A value the context manager yielded is
  therefore no longer usable in a later stage.
- A `parallel` stage whose iteration failed was reported skipped or xfailed when another
  iteration, still running, then called `pytest.skip()` or `pytest.xfail()` from a user function,
  and failed with that iteration's message instead of its own on a `pytest.fail()`. Those are now
  secondary to the stage's failure, as any failure of the other iteration's own already was.
- The HAR export has every request a stage sent. Of a `parallel` stage it left out those of the
  iterations that failed, were cancelled, or skipped, xfailed or failed from a user function after
  another iteration had ended the stage, and a stage a user function's `pytest.skip()`,
  `pytest.xfail()` or `pytest.fail()` ended had no entry at all, not even the request the function
  answered. They went on the wire all the same: the HAR file now records them, the other
  iterations' first and the exchange the report shows last.
- A scenario that is not UTF-8, or that `$include`s a file that is not, fails to load with a
  message naming that file. On a Latin-1 file, `pytest-httpchain resolve`, `show` and `graph`
  printed a raw `UnicodeDecodeError` traceback, and `validate` and collection reported the
  catch-all `HTTPCHAIN015` "Failed to parse JSON file" with only the codec's complaint, so a bad
  included file could not be told from a bad scenario. It is now `HTTPCHAIN014`, like any other
  invalid JSON (`Invalid JSON: .../common.json is not valid UTF-8: ...`), and the three commands
  print that message and exit 1. An integer longer than Python converts (4300 digits by default)
  failed the same way and is now `HTTPCHAIN014` too (`.../common.json cannot be parsed: Exceeds
  the limit ...`). A reference path the operating system rejects, such as
  `"$include": "a\u0000.json"` or a lone surrogate, also printed a traceback; it is now
  `HTTPCHAIN012` (`Reference path contains a NUL character: 'a\x00.json'`). A scenario, or a file
  it includes, nested too deeply to parse stays `HTTPCHAIN015`, now worded "nested too deeply",
  and `resolve`, `show` and `graph` print one `error:` line for it and exit 1 instead of a
  traceback.
- A UTF-8 file that starts with a byte-order mark, as some editors on Windows save one, is no
  longer rejected as invalid JSON (`Unexpected UTF-8 BOM`). Scenarios, the files they `$include`,
  `$merge` or `$ref`, and `verify.body.schema` files all accept the mark.
- The HTTP report section and the HAR export show the body of a redirect follow-up that keeps the
  original method. Such a follow-up was presented as a consumed upload: a plain `GET` after a `302`
  was reported as `<Streaming body (e.g. multipart file upload): consumed on send, not captured>`
  with HAR `bodySize: -1`, and a body re-sent by a `307` or `308`, or by a `301` answering a `PUT`,
  `PATCH` or `DELETE`, was missing from the follow-up's `postData`, though the server did receive
  it. httpx builds such a follow-up from the original request's body and never reads it; that
  body is plain bytes, so it is now read back from the request, with nothing sent again. A
  redirect that turns the request into a `GET` (a `302` or `303`, or a `301` answering a `POST`)
  was never affected. Only a plain-bytes body is read back, which a `files` or `multipart` body
  now is too (see the `files` entry under Changed), on the first request and on a `307`/`308`
  follow-up alike.
- A template holding more than one statement, such as the verify expression
  `{{ ok == True; False }}`, fails the stage instead of evaluating only its first part. simpleeval,
  which evaluates templates, stops at the first `;` and merely warns about the rest, so that
  expression came out `True` and the stage passed on half of what it checks. It now fails with
  `Invalid expression '{{ ok == True; False }}': a template holds one expression, not 2 statements
  separated by ';'`. A `;` inside a string literal is unaffected, and `validate` reports the
  template as `HTTPCHAIN037` (see below).
- An assignment in a template, such as the verify expression `{{ user.active = True }}` written
  for `==`, fails the stage instead of evaluating to its right-hand side. simpleeval evaluates `=`
  and `+=` that way behind a mere warning, so that expression came out `True` and the stage passed
  whatever `user.active` was. It now fails with `Invalid expression '{{ user.active = True }}': a
  template holds one expression, not an assignment; to compare two values, write '=='`. An
  augmented or annotated assignment, `:=` and any other statement in a template fail too, each
  with a reason of its own instead of simpleeval's (`Sorry, AnnAssign is not available in this
  evaluator`, `Sorry, 'import' is not allowed.`), and a syntax error with Python's reason alone
  (`invalid syntax`, without `(<unknown>, line 1)`).
- `validate` and pytest collection report a template the engine refuses from its text alone, with
  the reason the stage fails with, as `HTTPCHAIN037` in a stage and `HTTPCHAIN038` at scenario
  level or in a parametrize value (see Added), and that is its only finding. They reported nothing,
  or undefined names, for what fails every run, such as `=` written for `==` or a dict literal whose
  `}` runs into the template's closing `}}` (`{{ {'a': 1}}}`). An expression that did not parse was
  read for names as a regex's identifiers, so `{{ response.status == 200 and True) }}` also had
  `and`, `status` and `True` reported as undefined variables (`HTTPCHAIN003`), and one in a
  scenario-level template the error `HTTPCHAIN017` over the words of its string literals; such a
  template still fails `validate` and collection, as `HTTPCHAIN038`. A lambda's parameters are no
  longer read as names a template defines. A template nested too deeply for Python's parser to read
  (a few thousand `-` signs) crashed `validate` and collection with a `MemoryError`, and one holding
  a lone surrogate (a `\ud800` escape in the JSON) with a `UnicodeEncodeError`. Both are reported
  the same way now, and messages write the surrogate as its escape, which any terminal can print.
- A value that cannot be turned into text fails the stage with a message naming where it was
  used: the template it is interpolated into, or the query parameter it is the value of.
  `"{{ server }}/items?n={{ 2 ** 100000 }}"`, a number past the 4300 digits Python converts to
  text, or a value whose `__str__` raises, failed the stage with the raw `ValueError` (or whatever
  `__str__` raised) and the plugin's internal traceback, without naming the template. It now
  reads like any other failing expression:
  `ValueError in expression '{{ 2 ** 100000 }}': Exceeds the limit (4300 digits) ...`. The same
  template as the whole value of a query parameter, `"params": {"n": "{{ 2 ** 100000 }}"}`, keeps
  the number as a number, which is turned into text only when the request is built; that failed
  the same raw way and now fails with `Cannot convert query parameter 'n' to text: ValueError: ...`.
- The HAR export records a request's query string in the order the URL carries it. A repeated
  name's values were grouped under its first occurrence, so `?a=1&b=2&a=3` was recorded as `a=1`,
  `a=3`, `b=2` in `queryString`; form `postData` params already kept their order.
- A `parallel` stage is no longer held to 100 requests in flight whatever its `max_concurrency`.
  The shared client kept httpx's default pool of 100 connections, so the rest of the iterations
  waited for a free connection: 150 concurrent requests to an endpoint answering in a second took
  over two seconds, and a load test measured the pool rather than the server. The pool now has no
  connection limit unless the new `client.max_connections` sets one, leaving `max_concurrency` to
  bound the connections. This is over HTTP/1.1. Under HTTP/2, which the client offers by default
  and an HTTPS server may negotiate, all the requests to that server share one connection, which
  httpx holds to 100 requests at once (fewer if the server allows fewer) whatever the pool: a
  stage that needs more in flight sets `client.http2` to `false`.
- A body schema file (`verify.body.schema: "./schemas/x.json"`) whose meta-check crashes now
  fails the stage with `Invalid JSON Schema in file '...'`. The meta-schema's `format: regex`
  check expects only `re.error`, so a `pattern` that `re.compile` rejects some other way escaped
  raw: `a{4294967296}` raised `OverflowError` and about 1000 nested groups raised
  `RecursionError`. A schema nested a few hundred levels deep overflowed the meta-validator's own
  recursion the same way. The stage failed with a bare traceback, with no request/response report
  and no HAR entry. Inline schemas and `validate --deep` already reported these cases cleanly.
- The HTTP report no longer renders a deeply nested JSON body in full only to cut it to 1,000
  characters. Indentation grows with depth, so the full rendering is quadratic in the body's size:
  a 10 kB body 5,000 levels deep rendered to 50 MB. Rendering now stops at the cap.
- `validate`, pytest collection, `show` and `graph` no longer crash with a `RecursionError` on a
  scenario value nested a few hundred levels deep, such as a `vars` value, a query parameter or a
  `parametrize` value. The file loaded fine, but the checks that find template references,
  templated keys and scenario directives walked it recursively, two stack frames per level, and
  overflowed near 450 levels. They now walk it iteratively, so any depth the loader accepts is
  checked. So does model validation, which converted a `json` request body, an inline
  `verify.body.schema` or a `verify.jmespath` operand recursively and crashed on one nested past
  Python's recursion limit.
- Running a stage with such a value no longer crashes either. Substituting templates in a nested
  `vars` value took two stack frames per level, and stages failed from about 480 levels with a
  bare traceback. It now takes one frame per level. A value nested past the interpreter's
  recursion limit fails the stage with "Value nested too deeply to substitute".
- A `base64` request body whose template rendered text holding another template, which its
  template branch accepts, crashed the run with a traceback instead of failing the stage; it now
  fails the stage, `The base64 body is not valid base64`, the text ASCII or not. So did a
  `binary` or `files` path holding a NUL character (a JSON `"\u0000"`, or a template's), which
  the filesystem refuses: it fails the stage as a file that cannot be read.

### Changed

- **BREAKING**: three scenario mistakes that let pytest-xdist split a chain now fail collection
  in every run, with or without xdist, and `validate` reports the scenario as invalid: a stage name
  containing `::` (`HTTPCHAIN032`), an `xdist_group` in a stage's `marks` that the scenario does
  not declare (`HTTPCHAIN031`), and a scenario `xdist_group` name with a `]` after its last `@`
  (`HTTPCHAIN033`). Such a scenario used to collect and, without xdist, pass. Rename the stage
  (`Users: list`), move the `xdist_group` to the scenario's `marks`, or take the `]` out of the
  group name.
- **BREAKING**: `format` in a `verify.body.schema` is now enforced. It used to be ignored: the
  documented `{"type": "string", "format": "email"}` accepted `"not-an-email"`, and a stage whose
  response broke any `format` still passed. The body is now validated with the schema dialect's
  format checker, so a nonconforming value fails the stage (`'not-an-email' is not a 'email'`).
  Heads-up: a scenario whose responses never matched their declared formats, and that passed
  until now, fails after the upgrade. Out of the box `email`, `idn-email`, `ipv4`, `ipv6`, `date`,
  `uuid`, `regex` and `idn-hostname` are checked. `regex` means Python `re` syntax, not ECMA-262:
  a response that returns valid JavaScript patterns such as `(?<year>\d{4})` under
  `"format": "regex"` also fails now. `date-time`, `time`, `hostname`, `uri`, `iri`,
  `duration` and the other formats that need jsonschema's optional dependencies are checked only
  once `jsonschema[format-nongpl]` (or `jsonschema[format]`) is installed, and pass any value
  until then.
- **BREAKING**: a factory fixture's context manager is exited at the end of the stage that
  entered it, not when the scenario is done, and an exception raised on exit fails the stage
  (see Fixed). Heads-up: a scenario that saved a value the context manager yielded and used it in
  a later stage now gets it after the exit (a closed connection, say), and one whose exit raised,
  which passed until now with the error only logged, fails after the upgrade. Call the factory in
  each stage that needs the resource, or, to share one across stages, provide it from a
  `class`-scoped fixture.
- **BREAKING**: a backslash right before `{{` escapes the braces (see Added). `\{{ x }}` in a value
  rendered a backslash, then the value of `x`; it now renders the text `{{ x }}`, and `x` is not
  evaluated. Heads-up: to keep a backslash before a template, as in a Windows path
  (`"C:\\{{ dir }}"` in the JSON file), double it (`"C:\\\\{{ dir }}"`), or write the path with
  `/`. Every run of backslashes right before `{{` is doubled the same way: `\\{{ x }}`, which
  rendered two backslashes and the value, now renders one. `client.base_url`, `client.proxy` and a
  `verify.body.schema` file reference holding a backslash before `{{` fail validation. A key or a
  `functions` kwarg holding one, never rendered, is sent as before, and now warned of
  (`HTTPCHAIN029`, `HTTPCHAIN030`).
- Report sections and header checks' failure messages redact credentials by default (see Added).
  Heads-up: a tool or test that read a token back from a report, or matched a header check's
  message on a cookie's value, sees `[REDACTED]` after the upgrade; set `httpchain_redact_headers`
  and `httpchain_redact_query_params` to an empty value to get the previous output.
- The shared client opens as many HTTP/1.1 connections as the requests in flight need (see
  Fixed). Heads-up: a `parallel` stage with a `max_concurrency` above 100 now really runs that
  many requests at once against a server that speaks HTTP/1.1; set `client.max_connections` to
  keep a server from seeing more connections than it did.
- The `httpx` floor is raised to 0.28.0, the first release that takes the `socks5h://` proxy URLs
  the new `client.proxy` accepts (see Added). httpx 0.27.0 also percent-encoded a `\` in a URL
  path, so `{{ server }}/a\b` reached the server as `/a%5Cb` there, not as written (see Fixed).
  Heads-up: an environment that pins httpx below 0.28 has to lift the pin to upgrade.
- A verify step runs all its checks and reports every one that failed, where it stopped at the
  first, so fixing a scenario took one run per wrong assertion. Each header, header matcher field,
  `jmespath` entry and matcher key, expression, user function and `body` operand is a check of its
  own. One failure reads exactly as before; several are counted, `3 verification checks failed:`,
  above one numbered line each, in the order the checks ran, where a user function is named by its
  import name and index (`Function 'checks:is_valid' (user_functions[1]) verification failed`). A
  check that cannot run fails once: a body that is not JSON is one failure however many `jmespath`
  entries and `body.schema` wanted it, and a `jmespath` expression that cannot be evaluated is one,
  not one per key of its matcher. The step's templates still all render before its first check
  runs, but each check's on its own, so one that fails to render, an expression raising `KeyError`
  on a missing header or a value that rendered to `null`, is one failure in its check's place,
  where it ended the step before any check ran. Steps still run in order, and a step that fails
  still ends the stage, since a later one may depend on it; a `save` step still stops at its first
  error. Heads-up: a verify user function now runs even when a check before it in the step failed,
  so one that assumed they held (calling `response.json()` on what a failed `status` check let
  through) adds its own error to the list. Its `pytest.skip()` or `pytest.xfail()` ends the step
  without skipping the stage then, as a failure found before it stands, and so does a template of
  the step that did not render or called `pytest.fail()`, wherever it is; a `pytest.fail()`
  message is listed with the others. Heads-up too for a function a verify template calls that
  skips or xfails (`"expressions": ["{{ skip_unless_ready() }}"]`): it skipped the stage whatever
  the step's checks would have found, since the whole step rendered before any check ran. Its
  outcome now takes effect when the checks reach that template, so a check before it that failed
  fails the stage instead.
- A `$ref` in a `verify.body.schema` is resolved by the plugin, not by jsonschema's default
  registry, which fetched an `http(s)` reference over the network (with a `DeprecationWarning`) and
  failed on a local file. A reference to a local file now resolves (see Added), and a remote one
  fails the stage as unresolvable, naming it. Heads-up: a schema that referenced a hosted schema
  needs a local copy of it, and so does one whose root `$id` is a URL and whose relative
  references meant documents published beside it (`"$ref": "address.json"` under `"$id":
  "https://example.com/schemas/user.json"`): they resolve against the `$id`, as before, to a
  remote document. Drop such an `$id`, or make it relative, and the reference names the file beside
  the schema.
- `HTTPCHAIN028` no longer flags a `$ref` to a file inside an inline `verify.body.schema`: such a
  reference now resolves (see Added). It still flags `$include` and `$merge`, which JSON Schema
  does not have.
- The error for a schema `$ref` that does not resolve names what it looked for, `Cannot resolve
  a reference in inline body schema: $ref '#/$defs/missing' points to nothing in the inline
  schema`, where it quoted the whole document it looked in, an OpenAPI document included.
- A `verify.body.schema` file reference is kept as written, where it was read as a path: a
  `Path` folds the `//` and the trailing `/` a JSON pointer can hold. The editor schema describes it
  as a plain string without `format: path`, with examples of both forms. Heads-up: the first `#`
  now ends the file's path, so a schema file whose path holds a `#` (`schemas/v#1/user.json`)
  cannot be named, even percent-encoded; rename it. A path that starts like a URI, with `http:`,
  `https:` or `file:`, or a scheme and `//`, is refused as one at load; `./` in front keeps it a
  path (`./http:v1/user.json`). Any other colon is a path's, as it was (`schemas:v1/user.json`).
- A `vars` substitution step now builds the template evaluator once, not once per variable.
  Building it is a full pass over the context, so a step cost its number of variables times the
  size of the context, even for variables that hold no template. Values without a template are now
  kept as they are, without a walk. 300 variables against a 5,000-name context took about 0.3 s
  and now take about 5 ms, or well under 1 ms when no value is a template. A list value without a
  template is now the scenario's own list, not a fresh copy each time the step runs, as a JSON object
  value already was. A template or user function that changes it in place now changes it for later
  runs too.
- A `files` body is sent as the bytes it is encoded to, so a failing stage's report shows it and
  the HAR export records it. httpx streamed it, which neither could read back: the report said
  `<Streaming body (e.g. multipart file upload): consumed on send, not captured>`, the HAR entry had
  `bodySize: -1` and no `postData`, and the curl command asked for the parts to be added with `-F`.
  The report now shows a multipart body part by part, each part's headers and its content, or a
  binary part's size in place of it, where a single binary file made the whole body one
  `<Binary content>`; the HAR entry has it as sent, base64-encoded when a part is binary; the
  curl command sends it with the `Content-Type` naming its boundary, as it sends any other body.
  The placeholder for a body that is still a stream now reads `<Streaming body: not captured>`.
  What goes on the wire is unchanged but in three cases. `"files": {}` sent no body at all, and
  now sends a multipart body without parts, as `multipart` does when its lists render empty. A
  stage's own `Content-Type` of another multipart type naming a boundary (`multipart/mixed;
  boundary=abc`) now has the parts delimited by that boundary, where httpx took one from
  `multipart/form-data` alone and delimited them with a boundary the header did not name. And a
  stage's own multipart `Content-Type` naming no boundary (`multipart/form-data`) is sent with the
  body's added, where it named none for the server to find the parts by; one naming an empty
  boundary, or one the parts cannot be delimited by as written (`boundary="a;b"`, which had them
  delimited by `a`), fails the stage.
- A template is refused for anything in its text the engine does not evaluate, wherever in the
  template it sits: a lambda, a set comprehension, `*` unpacking outside a list literal, `yield`,
  `await`, an attribute named with a leading `_` or `func_` or one such as `format`, and a call of
  anything but a name or an attribute (`fns[0]()`). simpleeval refused each only once evaluation
  reached it, so `{{ a if ok else doc._id }}` rendered while `ok` held; it now fails the stage
  with a reason of its own (`the template engine does not read an attribute named '_id'; for a key
  of that name, write ['_id']`) instead of simpleeval's (`Sorry, access to __attributes ... is not
  available. (_id)`), and `validate` reports it (`HTTPCHAIN037`).
- `validate`'s text report over more than one file ends with a summary line, `3 files checked, 1
  with errors, 1 with warnings` (a file with errors counted under errors only; a path not found
  counted apart, as no file checked), and a directory given to it is searched for scenario files
  (see Added) where it was reported as `HTTPCHAIN011`, "Path is not a file". Files are reported
  sorted by path, in the text report and the `--format json` payload alike, where they followed
  the order they were given in: `validate b.http.json a.http.json` reports `a.http.json` first.
  The payload keeps its shape.
- A subscript of a key the object does not have, a saved object, `response.headers` or a `vars`
  object (see Added), fails the stage naming the key as a missing attribute does:
  `Key error in expression '{{ saved['nick'] }}': Key 'nick' does not exist in expression 'saved['nick']'`,
  where it read `KeyError in expression '{{ saved['nick'] }}': 'nick'`. A user function raising a
  `KeyError` is still reported as one. And a missing attribute of a `vars` object named like one of
  its methods (`keys`, `values`, `items`, `get`; see Added) says how to call it after the message
  it had:
  `Attribute error in expression '{{ order.items }}': Attribute 'items' does not exist in expression 'order.items'; the object has no key 'items'; to call its method, write .items()`.
  Heads-up: a test matching either old message in full sees the new one.
- An empty `vars` object (`{}`) is false, as an empty object saved from a response is, where it
  was true like any other: it reads as a mapping now (see Added). Heads-up: a template that tests
  one for truth renders differently: `{{ opts or defaults }}` takes `defaults` for an empty
  `opts`, `{{ 'a' if cfg else 'b' }}` renders `'b'`, and a stage whose template `always_run` names
  one (`"always_run": "{{ cleanup_opts }}"`) no longer runs after a failure. Test what is meant
  instead: `{{ opts is not None }}`, `{{ 'key' in opts }}` or `{{ len(opts) > 0 }}`.

## [0.15.2] - 2026-09-26

### Fixed

- `--dist loadgroup` keeps each scenario's stages on one worker again under pytest 9.2 (currently
  pytest's main branch). A pytest-xdist worker records an item's `xdist_group` by rewriting the
  private `item._nodeid` to end in `@<group>`, and the loadgroup scheduler groups by that suffix.
  pytest 9.2 derives `nodeid` from a structured id instead (pytest-dev/pytest#14758), so the
  rewrite no longer took effect. Every stage became its own work unit, and later stages failed
  on another worker without the earlier stages' saved values. The plugin now carries xdist's
  rewritten id over for scenario items. Nothing changes on pytest 9.1 and earlier.
- `--dist loadgroup` no longer fails at random with "Different tests were collected between gw0
  and gw1" when a scenario has a parametrized stage (pytest 9.1 and earlier). The plugin recorded
  collection order in a dict keyed by the test item. On those pytest versions an item's hash comes
  from its nodeid, which the xdist worker rewrites to add the group suffix between the recording
  and the lookup. Whether a lookup still succeeded depended on each worker's random hash seed, so
  workers ordered a parametrized stage's instances differently. Positions are now keyed by the
  item's identity.
- A `parallel.foreach` step whose template resolves to another template string now fails the
  stage with a message naming the step. Both step kinds also accept a template, so the text
  passed re-validation: an `individual` step then ran one iteration per character, and a
  `combinations` step escaped as a bare `TypeError`. 0.15.0 closed the same gap for the numeric
  `parallel` settings.

### Changed

- The `simpleeval` floor is raised to 1.0.8, a security release that blocks sandbox escapes
  through `operator` module functions (`attrgetter`, `itemgetter`, `methodcaller`, `call`) and
  `os.exec*`/`os.spawn*`/`os.posix_spawn*`. As with `os.system` before, a template context
  holding one of those callables now makes template rendering fail.
- Build backend range raised to uv_build 0.12.x. CI's uv already built with 0.12 and only warned
  about the `<0.12` range. Dependabot now proposes `uv-build` range bumps in a PR of their own:
  grouped with the lock-only bumps, the range bump made Dependabot skip the whole grouped lock
  update every week since uv_build 0.12 was released.
- Development tooling: ty is a dev dependency locked in `uv.lock` (0.0.84), run as `uv run ty
  check`, so Dependabot now moves it like ruff. Locked dependencies refreshed,
  `astral-sh/setup-uv` moved to v10.2.0, and the devcontainer's node feature to 2.x, with
  Dependabot now tracking devcontainer features too.

## [0.15.1] - 2026-09-26

### Fixed

- The editor JSON Schema now accepts an `$include`/`$merge`/`$ref` object in place of any model
  field, not only where a whole named type or a root key is expected. `"response": {"$include":
  "common.json#/checks"}` (or a shared `substitutions` list, or a base `url`) resolved at runtime
  but was flagged by the editor.
- A marker using `**` unpacking (`xfail(**{"strict": True})`) is now rejected as `HTTPCHAIN019`.
  Its keyword arguments used to be silently dropped, turning a strict xfail into a non-strict one.
  `*` unpacking was already rejected.
- In the `{name: stage}` form of `stages`, the key now names a `Stage` instance too (Python API);
  it overrode only a dict's `name` before.
- A user-function name (`"module:func"`), an `httpchain_suffix` value, or a scenario file name
  ending in a newline is now rejected. All three were checked with `re.match` against a `^...$`
  pattern, and `$` also matches just before a trailing `\n`. The shared name pattern
  (`pytest_httpchain.userfunc.NAME_PATTERN`) is now anchored with `\A`/`\Z`, so its `match()`
  is as strict as its `fullmatch()`.

## [0.15.0] - 2026-09-17

### Added

- `HTTPCHAIN030` warns when a `functions` substitution's `kwargs` contain a `{{ }}` template. Those
  kwargs are deliberately passed to the function unrendered, so the template arrived as literal text
  with nothing reporting it — the same gap `HTTPCHAIN029` closes for templated dict keys.

### Fixed

- `verify.expressions` entries must now evaluate to a bool. Any truthy non-boolean previously passed
  silently, so `"{{ response.status }}"` against a 500 response asserted nothing. A scenario relying
  on truthiness now fails with an explicit error naming the offending type.
- A template `always_run` can now see the scenario substitutions it is documented to see. When the
  chain aborted before any stage body ran — a fixture error in the first stage — the context was
  still empty, so the cleanup stage died on an undefined variable instead of running.
- The validator checks each response step against the saves that have actually landed by that step,
  so a verify step referencing a name a *later* save produces in the same stage is now reported as
  `HTTPCHAIN004` instead of passing validation and failing at runtime.
- HAR filenames are built from an allow-list of characters and bounded to 255 bytes. Parametrize ids
  containing `?`, `*` or quotes previously made the write fail, and the file was dropped with only a
  log warning.
- A `parallel` setting that resolves to something unusable now fails the stage with a message naming
  the field and the value. `repeat`, `max_concurrency`, `calls_per_sec` and `max_rate_limit_delay`
  accept a template, and a template resolving to another template string satisfies the model but
  reached the engine as text — `int("{{ 2 }}")` then escaped as a plugin traceback. A resolved `0`
  for `calls_per_sec` also used to disable rate limiting silently rather than being rejected.

### Changed

- Request/response report sections are formatted only when pytest will actually render them (a
  failure, or `-rP`/`-rA`/`--xfail-tb`), instead of for every passing test.
- The generated JSON Schema no longer repeats a root property's description inside its `anyOf`
  branch.

## [0.14.5] - 2026-09-17

Nothing in `src/` changed, so upgrading from 0.14.4 changes nothing at runtime.
This release carries a test-suite and CI pass.

### Changed

- Test suite review follow-up. Coverage rose from 95.13% to 97.11% and the full
  suite runs in about half the time: it is almost entirely independent pytester
  sessions, so CI now passes `-n auto`. The regression floor is ratcheted from
  88 to 94.
- Scenario `ssl` is exercised against a server presenting a real certificate.
  `SSLConfig` was pinned only at the `httpx.Client` kwargs level, which cannot
  tell a correctly built context from one trusting the wrong thing — no
  handshake ever happened. Now `verify` against a trusted CA bundle, against an
  untrusted certificate, and with verification disabled, plus `cert` against a
  server demanding a client certificate, each with its negative control.
- `validate --deep` covers the diagnostics it emits that no test reached:
  `body.files` paths, the `(cert, key)` tuple form of `ssl.cert`, a missing
  schema file, a schema file that parses but fails its meta-schema, and
  `save.user_functions` signature checking. Rootdir detection is tested through
  `pytest.ini`, `tox.ini` and `setup.cfg` as well as `pyproject.toml` — it
  decides the CLI's `$ref` root, and only one of the four spellings was covered.
- Integration tests go through one `run_scenario` fixture, which now takes extra
  pytest arguments and can run in a subprocess, instead of three competing
  spellings of the same setup. `CLAUDE.md` documents the suite's conventions.

## [0.14.4] - 2026-08-28

### Changed

- Minimum `typer` is now 0.26.0 (was 0.16.0). Older typer imports
  `click.utils.get_binary_stream`, which current click deprecates — and since the suite runs under
  `filterwarnings = error`, that made the declared floor a claim the lowest-floors CI leg could no
  longer satisfy. 0.26.0 is the first release that stopped using it.

### Security

- Substituted values are no longer logged at INFO. `process_substitutions` logged `Seeded <name> = <value>` for every `vars` entry and every `functions` alias, so a `--log-cli-level=INFO` run wrote auth tokens (and anything else a substitution produces) into the captured-log section pytest attaches to failure reports — while the carrier's context dumps, which carry the same data, were deliberately guarded behind DEBUG. Names only, at DEBUG, with a regression test next to the existing context-dump ones.

### Added

- A CLI reference page in the docs (`Command Line`), covering all five commands and the previously undocumented `--direction`, `--version` and `--ref-parent-traversal-depth` options. `show`, `graph` and `resolve` had appeared exactly once on the whole site, under a heading about AI agents.
- `verify.expressions` carries a JSON Schema (a complete-template string) instead of emitting `items: {}`, so editors flag the mistake that matters most there — forgetting the `{{ }}`, which makes the assertion a non-empty and therefore always-truthy string. The runtime type is unchanged and the `HTTPCHAIN018` warning still covers files authored without a `$schema` key.

### Fixed

- pytest collection and `validate` no longer describe the same broken file differently. Both now report load failures through one taxonomy, so a malformed scenario is `[HTTPCHAIN014] Invalid JSON syntax` in either surface; collection used to answer with an uncoded "Cannot load JSON file", contradicting the diagnostics page's claim that only `HTTPCHAIN020`-`024` are CLI-only.
- `Diagnostic.location` is an indexed JSON path everywhere. Eight sites emitted the bare stage name, which is `""` for an unnamed stage — so `--format json` carried an empty string and the text output silently dropped the location. Now `stages[0].request`, `stages[0].response[1].verify.body.schema`, and so on, matching the sites that already did this.
- `HTTPCHAIN003` names the phase, not just the stage: "stage 's': substitutions references potentially undefined variable(s): ['wid']". A foreach parameter referenced from stage substitutions is the common case, and it *is* defined a few lines below — the phase is the whole explanation, and without it the warning reads as a typo report.
- `show` prints `fixtures: server, db` rather than the Python list repr `fixtures: ['server', 'db']`.
- `validate` no longer prints a schema diagnostic's location twice (once inside the message, once as the `(at ...)` suffix).
- The response half of a failure report calls a binary body `<Binary content: N bytes>`, matching the request half; the two sections are printed back to back and used two different spellings.
- Chain aborts now follow pytest's final item outcome rather than trying to predict it inside the stage body. Fixture setup/teardown errors and strict XPASS failures now stop later stages, false string `xfail` conditions no longer let genuine failures through, and skips plus genuine expected failures still continue as documented.
- HAR export preserves same-name response cookies with different domain/path scopes instead of raising `httpx.CookieConflict` and silently dropping the test's HAR file.
- Repeated URL-encoded form fields are emitted as separate scalar HAR `postData.params` entries, preserving their wire order and producing valid HAR instead of an array-valued parameter.

### Changed

- A diagnostic code's severity is declared once, in a `SEVERITY` map beside the `DiagnosticCode` enum, instead of being re-typed at all 37 `diag()` call sites. Tests assert the map covers every code and agrees with the table in `docs/diagnostics.md`, so a stray `"error"` can no longer promote a documented warning into a collection failure.
- Documented the scenario-scoped `httpx.Client`: cookies persist across stages with no `save` step (and across a parametrized scenario's runs, since the client is per test class), connections are pooled, and HTTP/2 is used when negotiated. Pinned with an integration example.
- Documented the real parallel-`save` semantics. The page said saves "may behave differently in parallel mode (last write wins)"; they merge in *iteration* order regardless of completion order, and a failing iteration commits nothing at all — a stronger guarantee than the hedge implied.
- `docs/diagnostics.md` no longer describes `HTTPCHAIN007`/`008` as body-only; both also fire on header matchers.
- The README leads with `$include`/`$merge` and names `$ref` as the legacy alias, matching the code and the rest of the docs — it previously documented only `$ref` and then recommended the VS Code `$schema` integration that `$ref` interferes with. Its Quick Start also opens with the JSON rather than a `conftest.py`.
- `CLAUDE.md` records all five commands CI's Lint job runs; it claimed four and omitted the committed-schema drift check, so any model change passed the documented checks and then reddened CI.
- Removed a completed implementation plan (and the `exclude_docs` rule whose only job was hiding it) from `docs/`, so the tree means "the published site" again.

## [0.14.3] - 2026-08-11

### Fixed

- The non-blocking pytest-main CI leg collects tests again. anyio ships a `pytest11` entry point that is auto-loaded purely because httpx pulls anyio in transitively; its plugin imports the `CallSpec2` alias that pytest main renamed, and `filterwarnings = error` promoted that deprecation to a hard error inside `load_setuptools_entrypoints` — so the job died before collection and had been reporting on **zero tests**. The suite never uses anyio's plugin, so the leg now runs with `-p no:anyio`.
- The three non-integer ini cases in `test_invalid_config` accept either wrapper's wording. Through pytest 9 the `type="int"` coercion is a bare `int()` whose `ValueError` the plugin catches and re-raises; pytest 10 raises the `UsageError` itself, so the plugin's handler never runs. Both embed `int()`'s own message, and `pytest.raises(pytest.UsageError)` still pins what matters — a clean usage error rather than an INTERNALERROR traceback.

### Changed

- No runtime behavior changes: the installed package is functionally identical to 0.14.2, the only edit under `src/` being a comment correcting a note about pytest's int-coercion handling.

## [0.14.2] - 2026-08-04

### Added

- `HTTPCHAIN029`: a `{{ }}` expression in a dict **key** (a header name, query parameter, or JSON body key). Only values are substituted, so a templated key went out on the wire verbatim — and it was equally invisible to `contains_template` and the data-flow scan, so nothing anywhere reported it. The four docs pages that promised template expressions "anywhere in your requests" now say values.

### Fixed

- A `verify.status` (or `verify.body.schema`) template that renders to `null` no longer silently drops the assertion. Both the pre- and post-render models validate cleanly because the fields are optional, and the check was gated on truthiness — so a stage whose only assertion rendered away **passed green against a 500**. Rendered-away assertions are now a stage failure naming the field; "never declared" and "declared but rendered to nothing" are no longer indistinguishable.
- Stage methods carry `__test__ = True`, so collection no longer depends on the user's `python_functions` ini. The generated `"test NN - <stage>"` names matched pytest's default only via its bare `test` prefix rule — the space defeats every glob — so a narrowed `python_functions = test_*` collected **zero** stages and left CI green with nothing tested.
- The split-chain selection warning no longer takes down the session. It is emitted from `pytest_collection_finish`, which — unlike every other warning site in the plugin — has no warning-to-error recovery, so under `filterwarnings = error` any `-k`/`--lf`/`--deselect` that orphaned a chain ended the run in an INTERNALERROR traceback with exit code 3. It now raises a clean `UsageError`, honoring the user's policy without crashing pytest.
- Stage failures print their message once instead of two to four times: `pytest.fail` was called from inside the `except` block, which set `Failed.__context__` to the original exception, and pytest walks the whole `__cause__`/`__context__` chain even under `pytrace=False` — and plugin errors and httpx transport errors are themselves chained.
- `validate` no longer disagrees with collection about the reference root. `resolve_root_path` took the *nearest* ancestor with any project marker, so a sub-package's bare `pyproject.toml` (a monorepo, a nested package) shrank the CLI's root below pytest's `rootdir` and rejected `$ref` targets that collection resolves fine — a red CI on a working scenario, given the README sells the command as a CI gate. A directory now counts only when it holds a file pytest would accept as its inifile (`pytest.ini`, or a `pyproject.toml`/`tox.ini`/`setup.cfg` carrying a real pytest section), with the old marker scan as fallback.
- `show` and `graph` see the name-keyed `response` mapping form. The raw steps were re-derived with a bare `isinstance(..., list)` instead of the model's own normalizer, so the whole mapping form was discarded: a consuming stage was reported as consuming nothing and its dependency edge vanished from the flowchart, making a genuine chain look safely reorderable. The normalization is now shared (`scoping.raw_list_entries`).
- A `$ref` rejected for escaping the root path says so, instead of reporting "not found" for a file that plainly exists on disk. The two rejection causes were folded into one boolean and one message.
- A non-UTF-8 JSON Schema file fails the stage cleanly. `UnicodeDecodeError` is a `ValueError`, not a `JSONDecodeError`, so it escaped the narrower `except` and passed untouched through the chain-abort machinery as a raw traceback — leaving the chain running, with no HTTP sections in the report. Same widening in `validate --deep`.
- `HTTPCHAIN028` no longer false-positives on standards-compliant inline JSON Schemas: any `$ref` not starting with `#` was flagged as a misplaced scenario directive, but JSON Schema `$ref` is a URI-reference, and absolute-URI and `$id`-relative refs resolve fine through the validator the runtime instantiates. Only relative `.json` file paths — what the scenario resolver would have handled — are flagged now.
- `HTTPCHAIN001` no longer fires on stages that simply omit the optional `name`: the model defaults it to `""`, so two unnamed stages collided on the default and schema-valid input was rejected with an error naming a field the author never wrote.
- HAR exports and failure reports no longer comma-fold repeated response headers. `httpx.Headers.items()` folds them, which RFC 6265 forbids for `Set-Cookie` precisely because cookie attributes contain commas — two cookies were corrupted into one unusable value.
- The template forms of `parametrize.individual` and `parametrize.combinations` are re-validated after resolving. Their model checks bail out ("values/keys unknown until runtime") and nothing re-checked the result, so heterogeneous combinations silently dropped every key missing from the first one, or failed with a bare `KeyError` naming neither the index nor the problem.
- `"{{ }}"` is no longer accepted as a complete template expression. It carries nothing to evaluate and simpleeval raises on the empty parse at runtime, while `validate` reported OK — and the partial-template validator had always rejected the same empty form.
- The coverage recipe in CLAUDE.md ran `coverage run -m pytest tests/unit`, but `fail_under = 88` applies to every `coverage report` and unit tests alone reach ~85 — so the documented sequence always exited non-zero on a clean checkout. It now runs the whole suite (as CI does), with an explicit `--fail-under=0` variant for the fast unit-only loop.

## [0.14.1] - 2026-07-28

### Added

- `pytest-httpchain --version` on the console script (standard eager typer callback); a bare invocation now shows help instead of a usage error.
- `validate` text output includes each diagnostic's model-path location (`... (at stages[0].response[1].verify)`), previously reachable only via `--format json`.
- HAR output and test reports now cover redirects: every redirect hop from `response.history` becomes its own HAR exchange, and the report labels the shown request with `(after N redirects)` when the final response followed redirects.
- Versioned editor-schema copies: `docs/schema/v<version>/scenario.schema.json` with an immutable per-release `$id`, accumulated in the repo and published with the docs; the unversioned URL keeps tracking latest. A Lint-job drift check regenerates the schema and fails CI if the committed copies are stale.
- CI: non-blocking test leg against pytest's main branch; least-privilege workflow `permissions`; per-ref `concurrency` cancellation for superseded PR runs; dependabot for GitHub Actions and uv.lock; codecov upload via OIDC with failures no longer suppressed.
- Collection warns when pytest selection (`--lf`, `-k`, `--deselect`, `--sw`) drops earlier stages of a chain while later ones stay selected — the survivors run without the deselected stages' saved context, previously failing with a bare undefined-variable error and no hint why.
- Templated function import names (`"module.{{ x }}:funcname"`) now actually work in `functions` substitutions: the name renders against the current context at seed time. The model has always advertised the form, but this call site passed it raw to the importer, so every invocation failed with `Invalid function name format`; the order-aware validator now also checks references inside these names.
- Integration coverage for previously untested documented features: multipart `files` upload (success path, report, and HAR), real end-to-end redirects (follow, `allow_redirects: false`, and the `(after N redirects)` report label), the four save-side error branches (raising/non-dict save functions, jmespath runtime errors, failing save substitutions), a raising request-level auth function, and the `teardown_class` re-run reset contract.

### Changed

- **Breaking (dependency surface):** pytest-order is no longer a runtime dependency and the plugin no longer injects `order(i)` marks — the plugin's own collection hooks have owned stage ordering since 0.12; the regroup still defends against a user-installed pytest-order acting on user-authored `order(...)` stage marks (covered by a new regression test).
- SSL wiring uses `ssl.SSLContext` instead of httpx-0.28-deprecated `verify=<str>`/`cert=...`: a CA-bundle path (file or directory) becomes `ssl.create_default_context(cafile=/capath=)` and client certs are loaded via `load_cert_chain`; scenarios setting `ssl.verify`/`ssl.cert` no longer trigger `DeprecationWarning` (a hard failure under `filterwarnings = error`). Plain `true`/`false` verify is unchanged.
- JSON Schema dialect selection is unified through `jsonschema.validators.validator_for` (new shared `json_schema_validator_class`): a schema without `$schema` now meta-checks against Draft 2020-12 — the dialect instance validation already used — instead of Draft 7; unknown `$schema` URIs fall back silently instead of logging; per-stage validation no longer re-runs the schema self-check.
- `DiagnosticCode` is a `StrEnum` and `Diagnostic.code` is typed with it, so unregistered codes fail at model-validation time; JSON output is unchanged.
- `--httpchain-output-dir` registers under a named `httpchain` group in `pytest --help` and its option dest is the properly-prefixed `httpchain_output_dir` (was the collision-prone bare `output_dir`); ini options register their real defaults via `addini` instead of a `None`-sentinel indirection.
- Schema generation converts tagged-union `oneOf`→`anyOf` via a `GenerateJsonSchema` subclass at generation time instead of a post-hoc document walk; pydantic then flattens directly nested `anyOf`s, so the emitted schema is simpler but semantically identical (committed schema regenerated).
- Template engine builds one simpleeval evaluator per `walk()` traversal instead of one per expression, per simpleeval's reuse guidance.
- pytest 9's `strict = true` umbrella is enabled; ruff gains the `PT` (flake8-pytest-style) family; coverage measures pytester subprocesses (`patch = ["subprocess"]` + `parallel`, so `coverage combine` before `report`); CI installs with `uv sync --locked`; publish/tombstones workflows split build from the OIDC-privileged publish job; build backend bumped to uv_build 0.11.x; the deprecated `License ::` classifier is dropped (PEP 639 expression only) and `Typing :: Typed` added.
- Integration test server runs the same Flask app on werkzeug's `make_server(port=0, threaded=True)` in a joined thread, replacing unmaintained http-server-mock — no bind/release port race, no 60-second `requests` busy-poll, and teardown no longer blocks on sleeping handlers. pytest-cov (unused) dropped from dev dependencies; trustme added for real throwaway PEMs in SSL tests.
- Tombstone placeholder distributions declare PEP 639 license metadata (`MIT` + LICENSE file), bumped to 0.9.2 for republication.

### Fixed

- Save-step `substitutions` were template-rendered twice: the carrier pre-walked the whole save model and `process_substitutions` then rendered each `vars` value again. This broke the documented strictly-in-order entry resolution within one save step (an entry referencing the previous entry's name failed with a misleading `Undefined variable`), and — worse — re-evaluated already-rendered values, so HTTP response text containing `{{ }}` (external data, not scenario code) was executed as a template expression with `env()` and every context callable in scope. Saves now render exactly once, in order; server data that looks like a template is saved literally.
- A multipart (`files`) request body no longer breaks reporting: httpx consumes the streaming body on send without buffering it, and reading `request.content` afterwards raises `RequestNotRead` — which degraded the report's request section to a formatting error and silently skipped the test's entire HAR file. Both paths now report the body as not captured (HAR `bodySize: -1`).
- A parallel stage is now cancellable: `KeyboardInterrupt` (or any unexpected error) escaping the iteration loop previously reached the executor exit, which ran **every queued iteration to completion** — a runaway `repeat: 10000` load test could only be stopped with SIGKILL. Queued iterations are now cancelled and in-flight ones stop before sending. Rate-limited iterations wait in an interruptible poll instead of a blocking acquire, so a stage failure no longer waits up to `max_rate_limit_delay` per in-flight thread while firing further side-effecting requests at the target; iterations that did complete after the failure are folded into the HAR so it reflects actual wire traffic.
- Scenario-level substitution templates are validated the way they resolve — strictly in order, entry by entry: a forward or same-entry reference (a guaranteed crash at scenario initialization that poisons every stage) is now reported as HTTPCHAIN017 instead of passing validation, and template-looking text inside `functions` **kwargs** (dead text, never rendered) no longer produces a collection-blocking false-positive error.
- The validator no longer crashes on markers pytest itself rejects with other exception types (e.g. the reserved `_name` → `AttributeError`): every parse failure becomes a clean HTTPCHAIN019 diagnostic.
- Parametrize `ids` are excluded from the data-flow template scan, matching the collection-timing predicate that already knew they are display-only — a template-looking id no longer emits spurious HTTPCHAIN003 warnings.
- `validate --deep` now import-checks user functions declared inside a substitutions-type save step, previously the one call-site family it skipped entirely.
- The failure report for a parallel stage no longer labels a successful iteration's exchange as `(failing of N ...)` when the failure carried no request info (template error, rate-limit timeout): it now says `(last completed of N ...)`.
- HAR `startedDateTime` is no longer fabricated at export time for redirect hops and the failed exchange — hops inherit their iteration's start and stage errors carry the real send time, so a failed request no longer appears to start after every successful one ended.
- Data-flow analysis (`show`/`graph`) tracks response steps in order with the stage's own saves shadowing as they land, matching the runtime's per-step layering: re-saving a variable and then referencing it no longer draws a phantom dependency edge on the earlier stage.
- `pytest_unconfigure` restores simpleeval's process-wide `MAX_COMPREHENSION_LENGTH` to what configure found, so in-process pytester runs (and nested sessions) no longer leak their cap into the enclosing process.
- Release gate unblocked: `publish.yml` grants its `test` caller job the `id-token: write` that the reusable test workflow's Codecov OIDC upload requests — a called workflow can never exceed its caller's grant, so every release run would have failed at plan time. The Codecov step is also skipped for fork PRs (GitHub never grants them OIDC; `fail_ci_if_error` made every external contribution red) and for the release-gate `workflow_call`.
- Versioned editor schemas are actually pinned: `generate_schema.py` no longer rewrites an existing `docs/schema/v<version>/` copy on every run (the pyproject version lags main between releases, so the "immutable" v0.14.0 URL had been silently repointed at unreleased content); the committed v0.14.0 copy is restored to the schema actually shipped in 0.14.0.
- `calls_per_sec` documentation (field description, generated schema, and the parallel guide) no longer claims the limit is "global across all workers": the limiter is per stage execution and per process, so consecutive stages, other scenarios, and pytest-xdist workers each get their own budget.
- Two JSON reads (verify-schema files in the carrier and the validator) used the platform locale encoding instead of UTF-8; `generate_schema.py` likewise writes UTF-8 with a stable trailing newline.

## [0.14.0] - 2026-07-22

### Added

- `validate --format json` payloads carry a top-level `strict` key, so consumers can tell the gate semantics of `valid` (which includes warnings under `--strict`) apart from each file's pure-validity `result.valid`.
- Retroactive CHANGELOG entry for 0.8.1 (ty adoption + readability pass), which was released without one.

### Changed

- Architecture: the collection-time test-class factory moved out of the runtime engine into its own module (`factory.create_test_class`); the user-function grammar moved to `constants` so `userfunc` sits above `models` and owns the model-aware `call_user_function` dispatch (previously stranded in `utils`); `check_scenario` is now an orchestrator over per-family diagnostic helpers; jsonref's stateless `RefPathHelper` class became module functions. Import-linter layers updated accordingly. No behavior changes.
- HAR filenames for pytest node IDs containing `/`, `\`, or `:` now carry a short digest suffix — sanitization mapped distinct IDs to the same `_`-separated name, silently overwriting one test's `.har` with another's.

### Fixed

- An inactive `xfail` mark (`xfail(False, ...)`) no longer smuggles a genuine stage failure past the chain-abort machinery: pytest reports such a stage as failed, and subsequent stages now correctly skip.
- The runtime HTTPCHAIN027 reserved-name warning surfaces as a clean stage failure under `filterwarnings = error` instead of a raw warning traceback that bypassed chain-abort.
- JSON pointer array indices follow RFC 6901 strictly: `-1`, `+1`, and whitespace forms are invalid-pointer errors instead of silently resolving via Python indexing semantics; pointer errors inside external fragments now name the fragment file.
- The connection-refused example scenario targets a dynamically bound-and-released port via a `closed_port` fixture instead of the static 59999, which Linux could legitimately hand to a running process; the integration assertion additionally tolerates the drop-to-timeout behavior of GitHub's Windows CI runners, which never refuse loopback connections to closed ports.
- `format_response` tolerates undecodable bytes served with a JSON content type (UnicodeDecodeError), matching the runner's own handling.
- Docs/metadata accuracy sweep: README's hosted-schema claim (tracks `main`, not the latest release), HAR size note, a pointer to the diagnostics reference; classifiers include Python 3.14; stale isort config entry removed; error subclasses documented; assorted docstring corrections (JsonModule, scoping's parametrize note, iteration parameter naming).

## [0.13.0] - 2026-07-22

### Added

- Published the full `HTTPCHAINxxx` diagnostic-code reference as a docs page (Validation Diagnostics), including a `filterwarnings` recipe for `ScenarioValidationWarning`; a unit test keeps the page and the `DiagnosticCode` registry from drifting in either direction.
- The order-aware validator now catches intra-list substitution forward references: stage substitution steps resolve strictly in order, so a step referencing a LATER step's name is a guaranteed runtime `TemplatesError` — flagged as `HTTPCHAIN004` ("before the substitution step that defines it") instead of validating silently.

### Changed

- Context dumps in the runner ("global/local context on start", "updates for global context") moved from INFO to DEBUG, behind an `isEnabledFor` guard. They serialize every saved value — chained auth tokens included — and pytest attaches captured logs to failure reports, so they are now opt-in; the guard also stops every stage from paying O(context size) JSON serialization when the level is off, and an unserializable (circular) saved value degrades to a placeholder instead of breaking the stage.
- HAR entries now carry each request's **actual** start time in `startedDateTime` (captured per iteration, after the rate-limit acquire) instead of a timestamp fabricated at report-write time — HAR waterfalls of parallel/rate-limited stages were fiction. `write_har_file` exchanges are now `(request, response, started)` triples.

### Fixed

- `$include`/`$merge` sibling-merge equality is now judged in JSON terms: Python's `True == 1` let a boolean/number pair at the same path merge silently (the sibling vanished), violating the documented no-silent-contradiction guarantee — such pairs are now a merge conflict. A scenario relying on the old silent behavior fails loading with `Merge conflict at <path>`.
- `show`/`graph` data-flow edges are computed per phase against the shadow rules `scoping` actually defines: stage substitutions and the `parallel` config resolve before iterations exist, so a `foreach` parameter no longer hides a genuine dependency on an earlier stage's save referenced there; substitution steps shadow cumulatively (a later step's name no longer hides an earlier step's dependency).
- A pyrate-limiter `Limiter` (owning an immortal leaker daemon thread) was created and leaked on every rate-limited stage execution; the limiter is now closed after the stage's iterations finish, and is not built at all for single-iteration stages (where it can never limit anything).
- The `httpchain_max_parallel_iterations` cap is enforced before the iteration list is materialized: a template-driven runaway `repeat`/`foreach` count used to allocate everything first and could OOM the process ahead of the very guard meant to catch it.
- Request/response report formatting: pretty-printed JSON bodies now honor the same 1000-character truncation cap as plain text (`_MAX_BODY_CHARS` promised it, but valid JSON of any size was included in full).
- `execute_stage`'s docstring documents the deliberate `xfail` exemption from chain-abort (an expected failure lets subsequent stages proceed); CLAUDE.md's lint commands now include the full CI gate (ty + import-linter) and the correct `httpchain_suffix` option name.

## [0.12.0] - 2026-07-22

### Added

- New `HTTPCHAIN028` warning diagnostic: a scenario reference directive inside an **inline** `verify.body.schema` is flagged — inline schemas are now standard JSON Schema and scenario directives are never resolved there (see the breaking change below). Flagged forms: `$include`/`$merge` with a string value, and a file-path `$ref` (a non-`#` `$ref` can never resolve in the runtime schema validator, so it is unambiguously a pre-0.12 leftover). A schema-internal `#/...` `$ref` is legitimate schema vocabulary and is never flagged.
- `pytest_httpchain.jsonref.load_json` accepts an `opaque` predicate over document positions; a matching subtree passes through resolution verbatim. Positions compose across file boundaries (content spliced in via a reference is judged at the reference site's position plus its fragment-relative path). The load pipeline uses it to keep inline verify schemas intact.

### Changed

- **BREAKING**: inline `verify.body.schema` values are now opaque to the reference resolver and treated as **standard JSON Schema, verbatim**. Previously the resolver intercepted `$ref` (and `$include`/`$merge`) anywhere in the document — so a real-world schema using `$defs`/`$ref` either failed collection with a pointer error or had scenario data silently spliced into it, and inline schemas behaved differently from external schema files (which were always loaded verbatim). Now `$ref`/`$defs`/`$schema` inside an inline schema are addressed to the schema validator, exactly as in the external-file form; an unresolvable schema-internal `$ref` fails the stage with a clean `VerificationError` (and aborts the chain) rather than an internal traceback. The opacity extends to sibling merging: two differing schemas arriving at the same position via `$merge` are a merge conflict, never blended. Scenarios that composed inline schemas via scenario directives must inline the shared content or switch to the file-path form (`"schema": "./shared.json"`); leftover directives inside schemas are flagged by the new `HTTPCHAIN028` warning.
- Dependency floors raised: `jsonschema>=4.18.0` (the first version with the `referencing`-based resolution machinery whose `Unresolvable` error the runner now catches), plus `referencing>=0.28.4` declared as a direct dependency (its exception type is imported directly). Enforced by the existing lowest-floors CI job.

### Fixed

- Multi-stage scenarios no longer interleave (and wipe each other's saved context) when two or more scenario files run in one plain, non-xdist pytest session. Every scenario class carried the same `order(0..n-1)` stage marks and pytest-order's default session-wide group scope stable-sorted equal indices across classes into A0, B0, A1, B1, … — with `teardown_class` resetting the chain state at every class switch, every stage after the first failed with a misleading "Undefined variable". The plugin now enforces its own invariant — each scenario class's items run contiguously, in stage order — after all other sorters, including tryfirst-wrapper sorters (pytest's `--ff`, pytest-order's `--order-after-ff`), and restores parametrized-instance order under shuffling plugins.
- The published editor schema's `pattern` constraints are now valid ECMA-262 regexes. They embedded Python's `(?P<name>…)` named-group spelling, which JS engines reject — VS Code's JSON language service silently dropped every pattern, so the promised editor-side flagging (e.g. of `"timeout": "abc"`) never happened.
- README (the PyPI long description) documented the pre-0.11 option spellings removed in 0.11.0; it now names the `httpchain_`-prefixed ini options and `--httpchain-output-dir`.
- The Windows CI job could report success while tests failed: the multi-line pwsh step only propagated the last command's exit code. The step is split so a failing pytest fails the job, and the tests that actually failed on Windows are fixed (glibc-specific network-error assertions and path-separator assertions on `str(Path)`).

## [0.11.0] - 2026-07-16

### Added

- Response metadata is now declaratively reachable in response steps via the reserved `response` namespace — `response.status`, `response.reason`, `response.headers` (case-insensitive), `response.elapsed_ms` — usable in `verify.expressions` (`{{ response.status == 200 }}`, `{{ 'json' in response.headers['content-type'] }}`) and in save templates, which makes saving a header a one-liner substitutions save. The namespace exists only inside response steps; the response **body** stays out (extract it with `save`). A user variable/save/fixture named `response` is shadowed there — the validator warns with the new `HTTPCHAIN027` diagnostic (and dynamically produced keys, e.g. from a `user_functions` save, trigger the same warning at runtime).
- `verify.headers` values accept matcher objects — `{"contains": ...}`, `{"not_contains": ...}`, `{"matches": ...}`, `{"not_matches": ...}` — besides the existing exact-match strings, so partial and pattern header assertions no longer require saving the header first. An absent header behaves as an empty string for the matcher forms, mirroring the body-check semantics. Contradictory matchers (same value in `contains` and `not_contains`) are a validation error, like their body counterparts.
- Ambiguous `$ref` lookups are now flagged. A relative reference path is looked up against the referencing file's directory first, then the root path; when a file exists under **both**, the file-relative one wins as before, but the resolver now emits `AmbiguousReferenceWarning` — surfaced by `pytest-httpchain validate` as the new `HTTPCHAIN026` warning diagnostic and at pytest collection as a `[HTTPCHAIN026]` `ScenarioValidationWarning` — instead of silently shadowing the root-relative file. The lookup order itself is now documented in the references guide.

### Changed

- **BREAKING**: `null` no longer bypasses the `$ref`/`$include` sibling-merge conflict rules. Previously a `null` on either side of a merged path was always accepted and the sibling silently won — so a sibling `null` could blank out any referenced value, contradicting the documented no-last-wins guarantee. Now `null` is a value like any other: pairing it with a different value at the same path fails loading with `Merge conflict at <path>` (two `null`s, like any equal values, still merge fine). The whole merge policy is now a single custom `deepmerge.Merger` whose conflict strategies raise, replacing the separate pre-merge conflict detector that had to be kept in sync by hand.

- **BREAKING**: dependency floors raised to match tested reality, now enforced by a lowest-floors CI job that installs every direct dependency at its declared minimum: `pytest>=9.0` (the `[tool.pytest]` configuration table is only read by pytest 9 — under 8.x the plugin mis-collected its own examples and `minversion` was silently unenforced), `pydantic>=2.13.4` (stable generated-schema output), `pyrate-limiter>=4.2.0` (the blocking `try_acquire` API the runner uses).
- Windows is now part of the CI test matrix. One portability fix came out of it: absolute `$ref` paths are judged under both POSIX and Windows path rules on every platform (previously `/etc/passwd` was not recognized as absolute when running on Windows; the root-containment check still applied, but the explicit rejection now matches on all hosts).
- HAR output (`--httpchain-output-dir`) for a parallel stage now contains one entry per iteration, in iteration order, instead of a single arbitrary iteration presented as the stage's only exchange. The `write_har_file` helper accordingly takes a list of `(request, response)` exchanges instead of one pair. (Exchanges are only retained for the HAR when the option is on, so runs without it keep the previous memory footprint.)
- The CLI's default `$ref` resolution root (`--root-path` unset) is now the auto-detected project root — the nearest ancestor containing a standard project marker (`pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`, `setup.py`, `.git`) — matching pytest collection, which sandboxes `$ref` to pytest's `rootpath`, so `validate`/`show`/`graph` accept exactly the references that collection accepts; pytest collection itself also routes through the same load pipeline as the CLI. A `$ref` that previously needed an explicit `--root-path` to reach a shared fragment above `tests/` now resolves by default. In a tree with no project marker at all (e.g. an exported scenario bundle), the previous default — the nearest `tests/` ancestor, else the file's own directory — still applies, so marker-less layouts keep working unchanged.

### Removed

- **BREAKING**: the deprecated pre-0.10 option spellings, completing the deprecation window opened in 0.10.0: the un-prefixed ini options (`suffix`, `ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) and the `--output-dir` flag. Use the `httpchain_`-prefixed ini names and `--httpchain-output-dir`; the old ini names now get pytest's standard unknown-option warning and the old flag is rejected as an unrecognized argument.

### Fixed

- A request that received no response (timeout, connection error) no longer vanishes from the diagnostics: the request that was on the wire is attached to the failure, shown in the report's `HTTP Request` section, and written to the HAR file as a status-`0` entry (the convention browser HAR exports use for aborted requests). Previously the HAR was silently skipped and no request section appeared.
- The report's `HTTP Request`/`HTTP Response` sections for a parallel stage now say which exchange they show — `(failing of N parallel iterations)` or `(last of N parallel iterations)` — instead of presenting one iteration as the stage's only exchange.

## [0.10.0] - 2026-07-16

### Added

- `request.method` accepts any RFC 9110 token, so non-enum verbs — WebDAV `PROPFIND`/`REPORT`, cache `PURGE`, vendor methods — are now representable; the standard verbs keep editor autocomplete. `verify.status` accepts any integer 100-599, so nonstandard codes (nginx 499, 599, vendor codes) can be asserted. Note: the editor schema no longer flags an unknown-but-token-shaped method (e.g. `FOOBAR`) as a typo, since it is now a legal value.
- Namespaced pytest options: `httpchain_suffix`, `httpchain_ref_parent_traversal_depth`, `httpchain_max_comprehension_length`, `httpchain_max_parallel_iterations` ini options and the `--httpchain-output-dir` flag. pytest ini options share one global namespace across all plugins, so the generic old names risked collisions.
- New `info` diagnostic severity: purely informational findings that never affect validity, are exempt from `--strict`, and produce no collection warnings. First code: `HTTPCHAIN025` — a stage's `parametrize` values contain `{{ }}` templates, which opts the scenario into collection-time resolution of scenario substitutions (the one exception to lazy initialization, see below).
- Docs: a "Resolution Phases" reference (collection / scenario initialization / stage execution) in the context-layering guide, describing what resolves in each phase, against which context, and how failures surface.

### Deprecated

- The un-prefixed ini option names (`suffix`, `ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) and the `--output-dir` flag. They keep working through the 0.10 series with a config-time deprecation warning and will be removed in 0.11; when both spellings are set, the `httpchain_`-prefixed one wins.

### Changed

- **BREAKING**: relative file paths in scenario fields — `body.binary`, `body.files` values, `verify.body.schema`, and `ssl.cert`/`ssl.verify` — now resolve against the **scenario file's directory** (the same rule as `$ref`/`$include`) instead of the pytest invocation directory. Data files can live next to the tests that use them, independent of where pytest runs from; suites that relied on CWD-relative paths must adjust them (absolute paths are unaffected). `validate --deep` file checks follow the same rule.
- **BREAKING**: a duplicate key in a JSON object now fails loading with `Duplicate key '...'` instead of silently keeping the last value. Plain JSON parsing made a duplicated organizational key — e.g. two `"check"` entries in dict-form `response` steps — silently delete the first one, weakening the test with no diagnostic.
- **BREAKING**: a user-function reference without a module path (`"auth": "myfunc"`) now fails at validation/collection with `Module path is required: use 'module:myfunc'` instead of validating and then failing at runtime import. Bare names never worked at runtime; note that a scenario carrying a bare name only in stages that never executed (e.g. permanently skip-marked) previously collected fine and now fails collection — like any other statically-detectable authoring error under strict validation.
- An unhandled model-union variant at any dispatch site (body types, save/verify steps, substitutions, parallel/parametrize steps) now raises loudly instead of being silently skipped — an "unknown body type sends an empty request" class of plugin bug can no longer pass unnoticed.
- **BREAKING**: pytest collection is now free of runtime side effects. Scenario-level `substitutions` resolution, the `auth` user-function call, `ssl` resolution, and httpx client construction are deferred from collection time to the moment a scenario's first stage executes. `pytest --collect-only` and IDE test discovery therefore no longer execute user code (e.g. live token fetches in `auth`) or allocate HTTP clients. Initialization runs at most once per scenario — side-effectful user functions are never re-invoked. A failure in it — an unresolvable scenario template, a raising `auth` function, a bad certificate path — surfaces as a clean `Failed to initialize scenario: ...` failure of the first executed stage, and every later stage (including `always_run` stages, and regardless of `xfail` marks) is skipped with the root cause as the reason, mirroring the previous behavior where a collection error meant no stage ran at all. Exception: when a stage's `parametrize` **values** contain `{{ }}` templates, scenario substitutions still resolve at collection, because pytest needs concrete parameter values to generate test items (templates in parametrize `ids` do not trigger this — they are never substituted). An initialization failure on an `xfail`-marked stage is reported as a real failure — the mark cannot absorb scenario-level breakage into a green run.

## [0.9.1] - 2026-07-14

### Added

- pytest-xdist support policy. Scenarios run in parallel correctly under the class-preserving distribution modes: `--dist loadscope`, `--dist loadfile`, and `--dist loadgroup` (every scenario class now gets an automatic `xdist_group` marker). Modes that distribute tests individually and would silently scatter a multi-stage scenario's stage chain across workers — `--dist load` (the `-n` default), `--dist each`, `--dist worksteal` — now fail collection with guidance instead. Single-stage scenarios have no chain and keep working under every mode.

## [0.9.0] - 2026-07-14

### Added

- Distribution now ships a `py.typed` marker and `[project.urls]` metadata.
- Import layering across the whole package is enforced in CI by an exhaustive import-linter layers contract: every top-level module belongs to exactly one layer, and a new module fails the check until placed.

### Changed

- **BREAKING**: Consolidated the six-distribution uv workspace into the single `pytest-httpchain` distribution. The former sub-packages are now subpackages/modules: `pytest_httpchain.models`, `pytest_httpchain.templates`, `pytest_httpchain.jsonref`, `pytest_httpchain.userfunc`; the shared exception base lives in `pytest_httpchain.errors`. Migration: install `pytest-httpchain>=0.9` only (drop any explicit `pytest-httpchain-*` requirements) and rename imports `pytest_httpchain_X` → `pytest_httpchain.X`. Scenario JSON files, user-function references, ini options, and CLI usage are unaffected.

## [0.8.1] - 2026-06-15

### Changed

- Adopted the `ty` type checker across the workspace: workspace-root `[tool.ty]` configuration with `error-on-warning` (targeted rule relaxations for the test suites), pinpoint `# ty: ignore[...]` suppressions in source, and a pinned `ty` step in the CI lint job.
- Readability pass from the 2026-06-15 audit: Google-style docstrings throughout, function-local imports hoisted to module level, and assorted naming/comment quick-wins (no behavior changes from the readability work itself).

### Fixed

- `call_function` no longer double-wraps a `UserFunctionError` raised by the user function itself (mirroring `wrap_function`), and the absolute-`$ref`-path rejection message was corrected.

## [0.8.0] - 2026-06-15

### Added

- New validation diagnostic `HTTPCHAIN019` (error): a scenario- or stage-level `marks` entry that is not a parseable pytest marker (e.g. `skip(` or an unsupported form like `foo.bar`). Markers were previously only parsed at collection time, so `validate` reported such a scenario as OK while `pytest` then aborted collection — `validate` now catches it up front, restoring it as a faithful pre-flight check.
- New validation diagnostic `HTTPCHAIN018` (warning): a `verify.expressions` entry that is a plain string with no `{{ }}` template. Such a value is always truthy at runtime, so the assertion silently passes — the validator now flags the likely forgotten braces.

### Fixed

- A malformed `request.body` shape (an unknown body-type key like `{"jsonn": …}`, an empty `{}`, or a non-object) — and the equivalent for the `save`, `substitution`, response-step, `parametrize`, and `parallel` discriminated unions — now raises a clean, located Pydantic `ValidationError` instead of a bare `ValueError`. The bare error was not a `ValidationError`, so it escaped every `except ValidationError` handler: `validate` aborted with a raw traceback (and emitted a crash dump on `--format json`), `pytest --collect-only` reported the error without naming the offending key, and `show`/`graph` dumped a traceback. All four now produce a coded diagnostic / clean message that names the bad key and lists the valid tags.
- A single-path SSL client certificate (`ssl.cert: "/path/to/client.pem"`) no longer crashes collection. The path was stored as a `pathlib.Path` and handed straight to httpx, which unpacks a non-tuple cert via `load_cert_chain(*cert)` and raised `TypeError`. Cert paths are now stringified for httpx, fixing both the single-path and `[cert, key]` tuple forms.
- A template inside an `ssl.cert` tuple (e.g. `["/certs/{{ name }}", "/certs/key.pem"]`) is now rendered. The template walker had no `tuple` case (and `cert` is the only tuple-typed field), so the `{{ }}` was passed to httpx verbatim. Tuples are now walked like lists.
- An empty `parallel.foreach` (`[]`) or empty `combinations` (`[]`) is now rejected at validation/collection time (`min_length=1`) instead of silently running the request once unparameterized (`foreach`) or failing with a runtime "produced zero iterations" error (`combinations`).
- A non-integer value for a numeric ini option (`ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) now produces a clean `pytest.UsageError` instead of an `INTERNALERROR` traceback. pytest's `type="int"` handling does a bare `int(value)` that raises `ValueError` before the plugin's range checks run; the reads are now wrapped to report a usage error.
- A plain JSON syntax error in a scenario file is now reported as `HTTPCHAIN014` ("Invalid JSON syntax") instead of the misleading `HTTPCHAIN012` ("JSON reference resolution error"), since no reference is involved. `HTTPCHAIN014` was previously unreachable because the resolver wraps syntax errors as `ReferenceResolverError`; the wrapped cause is now unwrapped to report the accurate code.
- `HTTPCHAIN017` is now listed in the validator module's diagnostic-code table (it was a live, firing code missing from the "full" table).
- The published editor JSON Schema now constrains the string branch of template-accepting fields, so an editor flags a non-template string that is also not a valid value for the field's concrete type — e.g. `timeout: "abc"`, `status: "not-a-status"`, `method: "FOOBAR"`. Templates, concrete values, and the stringified concretes the runtime coerces (`"30"`, `"200"`) remain valid, so no valid scenario is rejected. Runtime validation is unchanged.

### Changed

- PyPI keywords updated: dropped the stale `requests` (the project migrated to httpx in 0.2.0), added `httpx`, `http`, `api`, `integration-testing`.

### Removed

- The `--output`/`-o` option on the `schema` and `resolve` CLI commands. Both commands already default to stdout, so the option (and its `Wrote … to PATH` confirmation) only duplicated shell redirection. Redirect instead — `pytest-httpchain schema > scenario.schema.json` — which makes every command uniformly emit data to stdout.

### Documentation

- Corrected several copy-pasteable examples that failed at runtime: `{{ created_at is not none }}` → `is not None` (responses); subscript access on scenario `vars` (`{{ user['id'] }}`) → attribute access (`{{ user.id }}`) in the parametrization and comprehension examples, with a note that `vars` values are namespaces while fixture/`combinations` dicts use subscript.
- Documented that `verify.headers` are matched by exact, full-string equality (so `Content-Type: application/json` will not match `application/json; charset=utf-8`).
- Documented that the `usefixtures(...)` marker only triggers a fixture's setup/teardown and does **not** inject its value into the template context (use the `fixtures` array for that).
- Documented that `functions` substitutions seed a **callable** that must be invoked with `()` in templates, with usage examples.
- README: corrected the `$ref` paths note (absolute paths are rejected for security) and added a section documenting HAR export via `--output-dir`.
- Documented that `$merge` sibling keys are merged additively (they add keys; overriding an existing scalar is a conflict, not a silent override).
- Documented that a callable fixture value is wrapped as a factory and loses attribute access on the original object.
- Corrected the `max_parallel_iterations` cap docs: an over-cap stage fails at runtime, not at collection.

## [0.7.0] - 2026-06-14

### Added

- `--root-path` option on the `validate`, `resolve`, `show` and `graph` CLI commands to override the directory that constrains `$ref` resolution. The CLI defaults to the nearest `tests/` ancestor while pytest collection uses the repo root, so a `$ref` that resolves during collection can now be made to resolve the same way from the CLI.
- New validation diagnostic `HTTPCHAIN017` (error): a scenario-level `substitutions`/`auth`/`ssl` template references a name that is not a scenario-level substitution. Such references resolve at collection time against only the scenario substitutions, so anything else is a guaranteed collection-time crash — now caught up front with a coded, located diagnostic.

### Fixed

- A failing stage that does not use `parallel` is no longer reported as `Parallel execution failed at iteration 0`. Both the sequential and parallel execution paths share one error mechanism, and the wrapper that labels a failure with its iteration index was applied unconditionally — so an ordinary verify or request failure surfaced with a misleading parallel prefix and a meaningless `iteration 0`. The prefix is now added only when the stage actually configures `parallel`; otherwise the original error (e.g. `Status code doesn't match: expected 200, got 404`) is reported as-is.

- Request rate limiting (`parallel.calls_per_sec`) works again. The limiter was written against the pyrate-limiter 3.x API — it passed a `max_delay=` argument to the `Limiter` constructor and expected delay/bucket-full exceptions — but the project resolves pyrate-limiter 4.x, where that constructor argument was removed. As a result any stage that set `calls_per_sec` crashed with a raw `TypeError` at limiter construction, before a single request was sent, and the feature had no integration coverage to catch it. The limiter is now built with the 4.x API: each request waits up to `max_rate_limit_delay` seconds (default 60) for a slot and fails with a clean `Rate limit exceeded` error if none becomes available within that window. The `pyrate-limiter` dependency floor is raised to `>=4.0.0` to match the API the code now uses.

- A binary (`body.binary`) or multipart-file (`body.files`) request body that points at an unreadable path now fails with a clean error instead of a raw traceback. The body readers caught only `FileNotFoundError`, so sibling `OSError`s — a path that is actually a directory (`IsADirectoryError`), a permission denial (`PermissionError`) — escaped uncaught and surfaced as an internal pytest error that bypassed the normal abort/`xfail` flow. Both readers now catch `OSError` and report it as a `RequestError`; the existing missing-file messages are unchanged.

- Template error messages now show the expression in its real `{{ … }}` form and include the underlying cause. The message was built with an f-string that collapsed `{{ }}` to single braces, so a failing expression was reported as `'{ missing_var }'` — text that never appears in the user's scenario — and the specific simpleeval detail (which name/attribute) was dropped. Messages now render `'{{ missing_var }}'` and append the cause, e.g. `…: 'missing_var' is not defined`.

- A user module whose top-level code raises a non-`ImportError` (for example a `RuntimeError` while importing) is now reported as a clean `UserFunctionError` instead of escaping as a raw traceback. `import_function` caught only `ImportError`; it now wraps any exception raised during import, with the cause preserved in the message.

- A non-string reference value (e.g. `{"$ref": 42}`) now raises a `ReferenceResolverError` instead of a raw `TypeError`, so the plugin reports a clean collection error and the CLI exits non-zero with a message instead of crashing.

- The `schema`/`resolve` CLI commands now report a clean `error: cannot write …` and exit non-zero when `--output` points at an unwritable path (e.g. a missing directory), instead of surfacing a raw traceback.

- A failure while building a test class at collection time — resolving scenario-level substitutions/`ssl`, calling the scenario `auth` function, or constructing the HTTP client — is now reported as a clean collection error instead of a raw internal traceback, matching the load/validate paths.

- `show`/`graph` now attribute a re-saved variable to its most recent producer before the consumer, not the first one, matching the runtime `ChainMap` layering where a later save shadows an earlier one.

- The order-aware validator now models that stage `substitutions` and the `parallel` config resolve *before* any `foreach` iteration variable exists, so a substitution that references a `foreach` parameter is flagged (it fails at runtime) instead of being treated as valid.

- The `HTTPCHAIN002` fixture/variable-conflict check is now scoped per stage. A fixture used only in one stage and a same-named parametrize parameter used only in another stage never coexist and are no longer falsely reported as a collection error.

- Exported HAR files now report the real request duration. The HAR writer's timing parameters were never supplied at the call site, so every entry recorded `time: 0`; the duration is now taken from the response's elapsed time.

- HTTP response report sections no longer dump unbounded binary mojibake. A response with a non-textual content type is summarised as `<binary N bytes>`, an unreachable decode-error branch was removed, and the response body is truncated with the same limit as the request body.

- An exception raised by a user function or factory fixture invoked *inside* a `{{ }}` expression is now wrapped as a `TemplatesError` (with the cause in the message) instead of escaping as a raw traceback, honoring the documented all-errors-are-`TemplatesError` contract.

- Request report sections no longer mislabel a text body that merely fails JSON parsing as `<Binary content>`. The body is decoded first; only genuinely undecodable bytes get the binary placeholder, while text that fails to parse as JSON is shown as-is.

- Importing `pytest-httpchain-models` no longer mutates the process-wide Python warnings filters as an import side effect. The suppression of Pydantic's field-shadow warning (for the `json`/`schema` body fields) is now scoped with `warnings.catch_warnings()` to only the two model classes that need it.

### Changed

- `parallel.foreach` with an `individual` step now rejects a multi-parameter dict. Only one parameter per `individual` step was ever honored — extra keys were silently discarded — so a dict with more than one key now fails validation (with the offending location) instead of quietly dropping parameters.

- The main `pytest-httpchain` package now declares the dependencies it imports directly (`pytest`, `jmespath`, `jsonschema`, `simpleeval`, and the `pytest-httpchain-core`/`pytest-httpchain-userfunc` workspace packages), which were previously only resolved transitively through sibling packages.

- A failing parallel stage now commits no saved variables. Previously the saves collected from whichever iterations happened to finish before the error were committed to the global context, so which values survived a parallel failure depended on thread timing; a failed stage now leaves the global context unchanged (deterministic).

- A malformed marker on a *stage* now fails collection with a clear error instead of being silently dropped while the stage still runs — matching how an invalid scenario-level marker is already handled.

- The numeric ini options (`ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) are registered as integers, so a non-integer value is rejected by pytest with a clean message and an out-of-range value raises a pytest usage error instead of an `INTERNALERROR` traceback. The `pytest` dependency floor is raised to `>=8.4` (required for integer ini options).

- Context-variable names — `vars` keys, function-substitution aliases, and JMESPath save keys — must now be valid Python identifiers. A non-identifier key (e.g. `my-var`) could never be referenced in a `{{ }}` expression and is now rejected at validation instead of silently producing an unusable variable.

- The function-reference format (`module.path:function`) is validated more strictly: a malformed module path (e.g. `a..b:f`, `mod.:f`) is rejected at validation/collection time instead of failing later at import.

- `$ref` resolution no longer falls back to the current working directory. References resolve purely from the referencing file's directory and the configured root, so resolution no longer depends on where pytest was launched.

- An object that contains more than one reference directive (any two of `$ref`/`$include`/`$merge`) now raises an error instead of silently honoring one and discarding the rest.

- `wrap_function` in the `pytest-httpchain-userfunc` package dropped its unused `default_args` parameter; pass positional arguments at call time instead. `default_kwargs` is unchanged.

- The four workspace sub-packages (`pytest-httpchain-jsonref`, `-models`, `-templates`, `-userfunc`) now ship real READMEs as their PyPI landing pages, where previously each shipped an empty file.

### Security

- Absolute `$ref`/`$include`/`$merge` paths are now rejected. An absolute path bypassed the parent-traversal limit (it contains no `..`), so a scenario could reference a file anywhere on disk (verified by reading `/etc/hostname`); the resolver now rejects absolute reference paths outright.

## [0.6.0] - 2026-06-13

### Added

- New validation diagnostic `HTTPCHAIN009` (warning): a stage saves a variable whose name is also a scenario-level fixture. The fixture value takes precedence in every stage, so such a save can never be read back.
- New validation diagnostic `HTTPCHAIN016` (error): a fixture is referenced in a scenario-level template (`substitutions`, `auth`, or `ssl`). Those templates resolve once at collection time, before any fixture exists, so such a reference is a guaranteed collection-time crash — now caught by `validate` and collection-time validation instead.
- The order-aware data-flow validator now checks `always_run` template references against their actual evaluation scope — fixtures, parametrize parameters, scenario substitutions, and earlier saves (`HTTPCHAIN003`/`HTTPCHAIN004`) — and `show`/`graph` count them as variable consumption.

### Changed

- **BREAKING**: scenario models now reject unknown keys (every model derives from a shared `extra="forbid"` base). A misspelled field — `"headerz"`, `"alwaysrun"`, `"statu"` — fails validation at collection time with the offending key and its location, instead of being silently ignored and producing a wrong request. The documented `"$schema"` editor key keeps working: models discard it during validation, whether it sits at the top of the test file or at the root of a fragment pulled in by `$include`/`$merge`/`$ref`. A `"$schema"` inside plain data (an inline response-body JSON Schema, a JSON body) is preserved. Migration note: an undocumented pattern of stashing reusable nodes under a custom top-level key (e.g. `"definitions"`) for same-document `#/...` pointers is now rejected — move the stash to a separate fragment file and reference it with `file.json#/...` pointers.

### Fixed

- A whole-string template padded with surrounding whitespace now preserves its type. A value that is a single `{{ … }}` expression with leading or trailing whitespace — `" {{ a == b }} "` — was accepted by schema validation as a complete (type-preserving) template but evaluated at runtime as string interpolation, yielding a *string* instead of the typed value. For `verify.expressions` this was a silent false-negative: the result `" False "` is a non-empty, truthy string, so the assertion passed even when the expression was false; for `always_run` the stage always ran; for `repeat`/`timeout`/`max_concurrency`/etc. it produced a string where a number was expected. Runtime single-expression detection now uses the same whitespace-tolerant predicate as the models, so `" {{ a == b }} "` evaluates to the bool `False`. Note: the surrounding whitespace (spaces, tabs, newlines) is stripped from such whole-string templates — a value that previously carried a leading/trailing newline into its output via interpolation now returns the bare typed value.

- User-function error messages now include the underlying cause. When an `auth`, `save`, or `verify` function failed to import or raised at call time, the error read only `Error calling function '<name>'` / `Failed to import module '<path>'` — the real exception (a `KeyError`, a connection failure, the actual `ImportError`) was attached as `__cause__` but never shown, because stage failures are reported with `pytest.fail(..., pytrace=False)` and the validator embeds only the message text. The two wrappers now append `: {cause}` (matching the wrapper already used for `functions` substitutions), so the actual reason reaches the test output and validation diagnostics. In particular, a module that is missing now reads differently from one that exists but fails to import.

- Circular-reference detection no longer raises a phantom cycle when two documents reuse the same internal JSON pointer. Internal pointers (`#/a`) were tracked by pointer string only and inherited into the tracker used for external files, so a document referencing `#/a` whose subtree pulled in another file that referenced *its own* `#/a` failed to load with `Circular reference detected: #/a`. Internal pointers are document-local and are no longer carried across a file boundary; genuine internal cycles (within one document) and cross-document cycles (tracked by file + pointer) are still detected.

- The published editor JSON Schema now actually validates scenario files. Its `JsonRef` wrapper accepted *any* object (no required keys), so editors caught neither typos nor missing required fields anywhere an object was expected. A reference object must now carry one of `$ref`/`$include`/`$merge`; combined with unknown-key rejection above, editors flag misspelled fields as-you-type. Tagged unions are emitted as `anyOf` instead of `oneOf`, so a reference object at a union position (a `save` value, a request `body`, a `parallel` config…) is no longer rejected as ambiguous. The schema root also explicitly declares `$schema` and the three reference directives.

- `always_run` template expressions are now actually evaluated. Previously the runtime tested the raw field for truthiness, so any template string — e.g. `"always_run": "{{ should_run }}"` — behaved as `always_run: true` regardless of what it evaluated to. The template is now resolved (with Python truthiness) when an earlier stage has failed, against fixtures, parametrize parameters, scenario substitutions, and previously saved variables; a template that fails to evaluate fails the stage with a clear message instead of silently running it.

- Restored scenario-level `fixtures`: the documented top-level `fixtures` field (pytest fixtures available to all stages) had been silently dropped from the `Scenario` model in an earlier refactor — scenarios using it passed validation but failed at runtime with undefined-variable errors. The field is back in the model and the generated JSON Schema, fixtures are injected into every stage (deduplicated against stage-level `fixtures`), and `show`/`graph` report them from the model.
- The validator and `show`/`graph` no longer treat an undocumented top-level `vars` key as a variable source. The runtime never read it; with unknown keys now rejected, such a file fails validation outright instead of validating "OK" and failing at runtime. Scenario-level variables belong in `substitutions`.

## [0.5.0] - 2026-06-04

### Added

- New read-only inspection CLI commands: `pytest-httpchain schema` (emit the scenario JSON Schema for editor integration), `resolve` (print a scenario with `$ref`/`$include`/`$merge` inlined), `show` (summarize stages and variable data-flow), and `graph` (emit a Mermaid flowchart of the stage data-flow).

### Removed

- **BREAKING**: Removed the `pytest-httpchain install` command and the bundled skill-installation machinery, including `src/pytest_httpchain/skill.md`. The Claude Code authoring skill now lives in a dedicated Claude Code plugin.

## [0.4.0] - 2026-06-04

### Added

- **Order-aware data-flow validation**: the validator now tracks variable availability stage-by-stage. A variable referenced before the stage that saves it — or referenced in a stage's request when it is only saved in that same stage's response — is reported as a forward reference (`HTTPCHAIN004`), distinct from a plain undefined-variable typo (`HTTPCHAIN003`).
- New semantic checks: a `verify` step that asserts nothing (`HTTPCHAIN006`), and body checks that both require and forbid the same `contains`/`not_contains` substring (`HTTPCHAIN007`, error) or `matches`/`not_matches` pattern (`HTTPCHAIN008`, error).
- Every validation finding now carries a stable diagnostic code (`HTTPCHAINxxx`), a severity, and a source location.
- `pytest-httpchain validate --format json` emits machine-readable diagnostics for editor/CI integration.
- **Deep validation** (opt-in `pytest-httpchain validate --deep`): resolves user-function references (`module:func`) by importing them (`HTTPCHAIN022`), checks call signatures against the arguments each call site provides — including the framework-injected `response` for save/verify functions (`HTTPCHAIN023` unexpected argument, `HTTPCHAIN024` missing required argument) — and verifies that referenced files exist (`HTTPCHAIN020`) and schema files are valid (`HTTPCHAIN021`). Deep findings are warnings; `--syspath` adds import roots and `--strict` makes warnings fail the exit code. Because it imports user code, deep validation never runs at collection time.
- `--strict` flag makes any warning count toward a non-zero exit (useful in CI alongside `--deep`).

### Fixed

- Undefined-variable detection no longer reports comprehension loop variables or lambda parameters (e.g. `x` in `{{ [x for x in items] }}`) as undefined — they are local bindings, not context references.
- The validator now flags `parametrize` parameter *values* that reference stage-level substitutions, fixtures, or saved variables: those values are resolved at collection time against scenario-level substitutions only, so such references fail at runtime. (`parallel.foreach` values, resolved later against the full stage context, are unaffected.)

## [0.3.0] - 2026-06-03

### Added

- `pytest-httpchain validate <file>...` CLI command for validating scenario files (structure plus semantic checks); exits non-zero on failure, so it can be used as a CI gate.
- Semantic validation now runs at **pytest collection time**: semantic errors (duplicate stage names, fixture/variable conflicts) fail collection with a clear message, and issues (undefined variables, stages with no verify) are reported as `ScenarioValidationWarning`. `pytest --collect-only` validates an entire suite.

### Changed

- Scenario validation logic now lives in the main package (`pytest_httpchain.validation`) as the single source of truth.
- `pytest-httpchain install` now installs only the Claude Code skill (the `--skill`/`--mcp` flags are removed).

### Removed

- **BREAKING**: Removed the bundled MCP server — the `pytest-httpchain-mcp` package, the `pytest-httpchain mcp` command, and the `mcp[cli]` dependency. Scenario validation is now available through the `pytest-httpchain validate` CLI command and at pytest collection time.

### Fixed

- Undefined-variable detection no longer emits false positives for names injected by `parametrize`, `parallel.foreach`, or `functions` substitutions.
- Undefined-variable detection now flags references to response data (`response`, `status_code`, `body`, etc.) inside `{{ }}` templates, where they are not available — response values reach templates only via an earlier `save` step.

## [0.2.4] - 2026-04-02

### Added

- HAR export: the `--output-dir` pytest option writes each test's HTTP request/response exchange to a HAR file for inspection and debugging.
- A full MkDocs documentation site (`docs/`), replacing the single `USAGE.md` guide.
- A generated JSON Schema for scenario files, enabling editor autocomplete and validation.
- An `install` command and a bundled MCP server for AI code-assistant integration (the Claude Code skill plus optional MCP server config).
- New integration tests covering request/save/schema error paths (connection refused, invalid hostname, malformed JSON in save and schema verification).

### Changed

- User-function imports now also search relative paths, so functions can be referenced from modules alongside the scenario file.
- pytest markers declared in scenarios are now parsed with `ast.literal_eval` instead of the template/`simpleeval` engine, so marker arguments are interpreted as plain Python literals.

## [0.2.1] - 2026-01-09

### Added

- Stages can now be defined as a dict with stage names as keys, in addition to the existing list format
  ```json
  // List format (existing)
  { "stages": [{ "name": "login", "request": {...} }] }

  // Dict format (new)
  { "stages": { "login": { "request": {...} } } }
  ```

### Changed

- Stage `name` field is now optional (defaults to empty string)
- Improved type safety in MCP server variable extraction functions
- `CircularDependencyTracker.create_child()` now properly supports subclasses

## [0.2.0] - 2026-01-08

### Changed

- **BREAKING**: Migrated HTTP client from `requests` to `httpx` for improved async support and HTTP/2 capabilities
- **BREAKING**: Scenario format restructured - variables are now defined within `substitutions` array instead of top-level `vars` key
  ```json
  // Before (v0.1.x)
  { "vars": { "user_id": 1 } }
  
  // After
  { "substitutions": [{ "vars": { "user_id": 1 } }] }
  ```
- **BREAKING**: JMESPath extraction in response `save` block now uses `jmespath` key instead of `vars`
  ```json
  // Before (v0.1.x)
  { "save": { "vars": { "user_name": "user.name" } } }
  
  // After  
  { "save": { "jmespath": { "user_name": "user.name" } } }
  ```
- Template engine now powered by `simpleeval` for safer expression evaluation

### Added

- User functions can now be called directly within substitution expressions
- Improved template expression capabilities with `simpleeval` integration

### Removed

- Removed note about parametrization not being implemented (feature now available)

## [0.1.2] - 2025-08-16

### Changed

- Updated package metadata to use `License-Expression: MIT` header for PEP 639 compliance

## [0.1.1] - 2025-08-16

### Changed

- Fixed markdown formatting in README (replaced backslash line breaks with double-space line breaks)

## [0.1.0] - 2025-08-16

### Added

- Initial release
- Declarative JSON test scenario format
- Multi-stage HTTP test support with ordered execution
- Common data context for sharing variables between stages
- Jinja-style template expressions with `{{ variable }}` syntax
- JMESPath support for extracting values from JSON responses
- JSON Schema validation for response verification
- User-defined Python functions for:
  - Custom data extraction
  - Response verification
  - Custom authentication
- JSONRef support with `$ref` directive for scenario reuse
- `always_run` parameter for cleanup stages
- Pytest integration (markers, fixtures, plugins)
- MCP (Model Context Protocol) server for AI code assistant integration
- Optional `mcp` dependency for MCP server installation
- Configurable test file suffix (default: `http`)
- Configurable `$ref` path traversal depth

[Unreleased]: https://github.com/aeresov/pytest-httpchain/compare/v0.16.1...HEAD
[0.16.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.15.2...v0.16.0
[0.15.2]: https://github.com/aeresov/pytest-httpchain/compare/v0.15.1...v0.15.2
[0.15.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.14.5...v0.15.0
[0.14.5]: https://github.com/aeresov/pytest-httpchain/compare/v0.14.4...v0.14.5
[0.14.4]: https://github.com/aeresov/pytest-httpchain/compare/v0.14.3...v0.14.4
[0.14.3]: https://github.com/aeresov/pytest-httpchain/compare/v0.14.2...v0.14.3
[0.14.2]: https://github.com/aeresov/pytest-httpchain/compare/v0.14.1...v0.14.2
[0.14.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.1...v0.2.4
[0.2.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aeresov/pytest-httpchain/releases/tag/v0.1.0
