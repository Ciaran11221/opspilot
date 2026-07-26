\# OpsPilot Power Platform bridge



Wraps OpsPilot's existing Claude tool-use backend as a Power Platform

custom connector, callable from a Power Automate flow and, on top of that,

a Copilot Studio agent - so the same agent logic that runs in OpsPilot's

browser demo can also answer questions inside Teams.



\*\*Status: designed, not yet live-tested.\*\* The OpenAPI spec below is

validated against the real Swagger 2.0 schema and the `/api/chat-sync`

endpoint it describes is built and unit-tested in `backend/`. The Power

Automate flow and Copilot Studio topic steps are written from Microsoft's

current documented behavior, but haven't yet been clicked through in a

live Power Platform environment - environment access has been the

blocker, not the design. Treat the steps below as a build guide to follow

once that's sorted, not a guarantee every click lands exactly as written.



\## Architecture



```

New ticket (Teams/email)

&#x20;       |

&#x20;       v

Power Automate flow  --calls-->  Custom connector  --calls-->  OpsPilot backend (/api/chat-sync)

&#x20;       |                                                              |

&#x20;       v                                                              v

Copilot Studio agent  <--posts result back into Teams--            Claude API

&#x20;       |

&#x20;       v

Human approves/edits, then it's sent to the client

```



The trace panel / SSE streaming in OpsPilot's browser demo is deliberately

not part of this path - Power Automate and Copilot Studio consume a

single JSON response, so `/api/chat-sync` (see `backend/agent.py`'s

`run\_agent\_sync` and `backend/main.py`'s `/api/chat-sync` route) runs the

same agent loop and redaction layer to completion and hands back one

result instead of a stream.



\## Prerequisite: deploy OpsPilot somewhere reachable



Power Automate and Copilot Studio are cloud services - they can't call

`localhost:8420` on your own machine. Before any of the steps below will

actually work, OpsPilot's backend needs to be deployed somewhere with a

public HTTPS URL. The lowest-friction option is Azure App Service (free

F1 tier is enough for a demo):



```

az webapp up --name opspilot-demo --runtime "PYTHON:3.12" --sku F1

```



(Run from `backend/`, with an `az login` already done. Azure automatically

gives you a `https://opspilot-demo.azurewebsites.net`-style URL.) Whatever

host you end up with is the value that replaces

`REPLACE\_WITH\_YOUR\_DEPLOYED\_HOST` in `opspilot-connector.swagger.json`.



\## Step 1: import the custom connector



1\. In the Power Platform maker portal (`make.powerapps.com`), go to

&#x20;  \*\*Data > Custom connectors > New custom connector > Import an OpenAPI

&#x20;  file\*\*.

2\. Upload `opspilot-connector.swagger.json` from this folder.

3\. On the \*\*General\*\* tab, confirm the host field picked up your deployed

&#x20;  URL correctly (edit it if not - this is the one field the import step

&#x20;  is most likely to need a manual fix on).

4\. On the \*\*Security\*\* tab: this connector currently expects `api\_key` as

&#x20;  a field in the request body (matching OpsPilot's existing `ChatRequest`

&#x20;  shape), not as connection-level auth. That's simplest to import as-is,

&#x20;  but means every flow action call needs the key typed into the action's

&#x20;  inputs rather than Power Platform prompting for it once per connection.

&#x20;  \*\*Worth revisiting later\*\*: moving `api\_key` to a header (e.g.

&#x20;  `X-Api-Key`) and declaring it as an API Key security scheme in the spec

&#x20;  would let Power Platform manage it as a proper connection instead - a

&#x20;  cleaner setup, just not the one built here yet.

5\. Save, then \*\*Test\*\* the connector with a real Anthropic API key and a

&#x20;  simple message (e.g. `"How many accounts are currently suspended?"`) to

&#x20;  confirm it reaches your deployed backend before building anything on

&#x20;  top of it.



\## Step 2: build the Power Automate flow



A minimal flow that reacts to a new item and posts OpsPilot's answer into

Teams:



1\. \*\*Create > Automated cloud flow\*\*. Trigger: \*\*"When a new email

&#x20;  arrives (V3)"\*\* (Outlook) or \*\*"When an item is created"\*\* (SharePoint

&#x20;  list) - whichever matches how tickets actually land for a real test;

&#x20;  for a first demo, \*\*"Manually trigger a flow"\*\* with a single Text

&#x20;  input is the fastest way to prove the connector end-to-end before

&#x20;  wiring up a real trigger.

2\. \*\*Add an action\*\* > search for your connector's name (\*\*OpsPilot Agent

&#x20;  Connector\*\*) > \*\*Run OpsPilot agent\*\*.

&#x20;  - `message`: map from the trigger (e.g. the email body, or the manual

&#x20;    trigger's text input).

&#x20;  - `api\_key`: your Anthropic key (see the security note above - for now

&#x20;    this has to be pasted directly into the action, ideally via an

&#x20;    environment variable rather than hardcoded in the flow).

3\. \*\*Add a condition\*\*: check `status` from the connector's response

&#x20;  equals `ok`.

&#x20;  - \*\*If yes\*\*: add \*\*"Post message in a chat or channel"\*\* (Teams),

&#x20;    posting `answer` (and looping over `draft\_reports` if you want each

&#x20;    drafted artifact posted as its own message).

&#x20;  - \*\*If no\*\*: post `error` somewhere visible (e.g. a different Teams

&#x20;    channel, or an email to yourself) rather than failing silently.

4\. Save, then run a test with the manual trigger before switching to a

&#x20;  real trigger.



\## Step 3: build the Copilot Studio topic



This is the piece that puts OpsPilot in front of an end user

conversationally, rather than only firing on a flow trigger.



1\. In Copilot Studio, create a new agent (or open an existing one), and

&#x20;  add a new \*\*Topic\*\*.

2\. \*\*Trigger phrases\*\*: things like \*"check ticket SLA risk"\*, \*"find

&#x20;  inactive accounts"\*, \*"draft an offboarding ticket"\* - phrases that

&#x20;  plausibly map to what OpsPilot actually does, so the topic fires on

&#x20;  relevant requests rather than everything.

3\. \*\*Add a node\*\*: "Ask a question" to capture the user's actual request

&#x20;  text (if not already captured by the trigger phrase itself).

4\. \*\*Add an action node\*\*: call the Power Automate flow from Step 2

&#x20;  (Copilot Studio topics can call flows directly as actions) - or call

&#x20;  the custom connector directly as a plugin/action, if using a

&#x20;  Copilot Studio tier that supports that; the flow-as-action route is

&#x20;  the safer default and matches what's documented above.

5\. \*\*Add a message node\*\*: respond with the flow's output (`answer`),

&#x20;  formatted for chat rather than raw JSON.

6\. Test in the Copilot Studio test pane before publishing to a Teams

&#x20;  channel.



\## Honesty notes (matching the main OpsPilot README's style)



\- \*\*Not yet run against a live Power Platform environment.\*\* The

&#x20; Swagger spec is schema-validated; the flow/topic steps are written from

&#x20; documented Power Platform behavior, not confirmed by actually clicking

&#x20; through them end-to-end yet.

\- \*\*`api\_key` in the request body, not connection-level auth\*\* - a

&#x20; deliberate scope cut to match OpsPilot's existing design rather than a

&#x20; recommended production pattern. See the Security note in Step 1.

\- \*\*No live business ticketing/email system connection\*\* - same honesty

&#x20; boundary as the main README: this bridges to Teams/Power Automate, not

&#x20; to a real client's Jira/Zendesk/M365 tenant.

\- \*\*The human-approval step in the architecture diagram is a design

&#x20; choice, not enforced by any code here\*\* - the flow as described posts

&#x20; OpsPilot's answer for a person to read and act on; it does not

&#x20; automatically execute anything against a real system, matching

&#x20; `draft\_report`'s own "draft only" contract in the main backend.

