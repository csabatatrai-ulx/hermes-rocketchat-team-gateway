workspace "Rocket.Chat Team Gateway" "C4 model of the Rocket.Chat Team Gateway, a Hermes Agent platform plugin." {

    !identifiers hierarchical

    model {
        member = person "Team member" "Asks the bot in rooms, threads and DMs, and answers approval prompts."
        admin = person "Administrator" "Runs the bot: tool list, approvals, who may talk to it."

        rocketchat = softwareSystem "Rocket.Chat" "Open source team chat on the organization's own servers." "External"
        model = softwareSystem "Model provider" "Nous Portal or any OpenAI-compatible endpoint." "External"
        mcp = softwareSystem "MCP servers" "Optional tools the agent may use, such as room history or business systems." "External"

        hermes = softwareSystem "Hermes Agent with the Rocket.Chat Team Gateway" "AI agent that answers a whole team in Rocket.Chat." {
            gateway = container "Hermes gateway" "Sessions, agent loop, approvals and cron, with this plugin loaded as its Rocket.Chat platform." "Python" {
                transport = component "Inbound transport" "One DDP subscription per bot; an optional outgoing-webhook listener on 127.0.0.1 with a shared secret." "ddp_transport_mixin.py, webhook_mixin.py"
                decide = component "Decision chain" "Pure classify_inbound: mention, allowlist, ignored senders, dedup, command guard." "inbound.py, builtin_commands.py"
                context = component "Turn builder" "Room context since the asker's previous mention, threads, DMs, attachments." "history.py, room_session.py, media.py"
                send = component "Outbound" "Quote and mention, approval and confirmation buttons, duplicate guard, chunking." "adapter.py, approval_buttons.py"
                rest = component "REST client" "Rocket.Chat REST with retries, no redirects, deprecation logging." "rest_client.py"
                state = component "Duplicate suppression" "Message ids and reply hashes that survive a restart." "dedup.py, shared_state.py"
            }
            files = container "Plugin state" "Inbound ids, reply hashes, DDP cursor and status, under $HERMES_HOME/rocketchat-plugin-dedup." "JSON files" "Store"
            cron = container "Cron runner" "Scheduled jobs; delivers through the plugin's standalone sender when no gateway runs." "Hermes process"
        }

        member -> rocketchat "Chats in rooms and DMs, clicks approval buttons" "Web, desktop, mobile"
        admin -> hermes.gateway "Configures tools, approvals and allowlists" "config.yaml, .env"
        rocketchat -> hermes.gateway.transport "Delivers new messages" "DDP WebSocket or outgoing webhook"
        hermes.gateway.transport -> hermes.gateway.decide "Payload"
        hermes.gateway.decide -> hermes.gateway.state "Seen before?"
        hermes.gateway.decide -> hermes.gateway.context "Accepted mention"
        hermes.gateway.context -> hermes.gateway.rest "Room history, files"
        hermes.gateway.send -> hermes.gateway.rest "Answers, prompts, reactions"
        hermes.gateway.send -> hermes.gateway.state "Records sent replies"
        hermes.gateway.rest -> rocketchat "Reads history, posts answers and prompts" "REST, HTTPS"
        hermes.gateway.state -> hermes.files "Persists" "File I/O"
        hermes.gateway -> model "Model calls" "HTTPS"
        hermes.gateway -> mcp "Tool calls, optional" "MCP"
        hermes.cron -> rocketchat "Delivers scheduled results without a gateway" "REST, HTTPS"
    }

    views {
        systemContext hermes "SystemContext" "Where the plugin sits: a team, its own Rocket.Chat, and Hermes." {
            include *
            include member
            autoLayout lr 260 160
        }
        container hermes "Containers" "What runs, what it talks to, and what it writes to disk." {
            include *
            include member
            exclude admin
            autoLayout lr 220 120
        }
        component hermes.gateway "Components" "The plugin's parts inside the Hermes gateway." {
            include *
            autoLayout tb 220 140
        }

        styles {
            element "Element" {
                color #ffffff
                stroke #142c86
                strokeWidth 2
                fontSize 22
            }
            element "Person" {
                shape Person
                background #142c86
            }
            element "Software System" {
                background #3a5bd9
                stroke #2a46b5
            }
            element "External" {
                background #8a96b8
                stroke #6e7aa0
            }
            element "Container" {
                background #3a5bd9
                stroke #2a46b5
            }
            element "Component" {
                background #5b74c9
                stroke #4660b8
            }
            element "Store" {
                shape Cylinder
            }
            relationship "Relationship" {
                color #5b6b94
                thickness 2
                fontSize 20
            }
        }
    }
}
