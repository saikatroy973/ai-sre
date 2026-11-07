import yaml

from ai_sre.switch import specs


def test_roster_is_six_distinct_named_agents():
    names = [agent.name for agent in specs.AGENTS]
    assert names == [
        "incident-commander",
        "cluster-investigator",
        "telemetry-analyst",
        "change-analyst",
        "memory-researcher",
        "evidence-verifier",
    ]
    assert len(set(names)) == 6
    assert set(specs.AGENTS_BY_NAME) == set(names)
    assert specs.ROOM_AGENTS == tuple(names)


def test_registration_bodies_are_switch_agents_not_secrets():
    for agent in specs.AGENTS:
        body = agent.registration_body()
        assert body["name"] == agent.name
        assert body["connector_type"] == "custom-agent-protocol"
        assert body["integration_profile"]["connection_model"] == "session_addressable"
        assert body["integration_profile"]["message_exchange"] is True
        assert body["metadata"]["read_only"] is True
        assert body["overwrite"] is False
        assert "api_key" not in body


def test_only_the_commander_and_verifier_hold_exclusive_roles():
    exclusive = {agent.name for agent in specs.AGENTS if agent.exclusive_role}
    assert exclusive == {"incident-commander", "evidence-verifier"}
    for agent in specs.AGENTS:
        role = agent.role_definition()
        assert role["name"] == agent.role
        assert role["exclusive"] is agent.exclusive_role


def test_core_room_group_template_parses_and_links_the_four_rooms():
    document = yaml.safe_load(specs.core_room_template())
    assert document["version"] == 1
    assert document["group"]["name"] == "Kubernetes Operations"
    assert {room["name"] for room in document["rooms"]} == set(specs.CORE_ROOMS.values())
    assert {link["to"] for link in document["links"]} == {
        "k8s-memory",
        "k8s-change",
        "k8s-governance",
    }
    for room in document["rooms"]:
        assert "bridge" not in room
        assert room["agents"]
        assert room["instructions"].strip()


def test_incident_template_parses_with_docs_roles_and_no_bridge():
    document = yaml.safe_load(
        specs.incident_room_template(
            room_name="incident-abc-shop-checkout",
            incident_id="abc",
            cluster="demo-cluster",
            namespace="shop",
            service="checkout",
            deployment="checkout",
            question="Why is checkout failing?",
        )
    )
    room = document["room"]
    assert document["version"] == 1
    assert room["name"] == "incident-abc-shop-checkout"
    assert "bridge" not in room
    assert room["agents"] == list(specs.ROOM_AGENTS)
    assert {doc["name"] for doc in room["docs"]} == {
        "incident-brief",
        "evidence-standard",
        "report-template",
        "memory-policy",
    }
    brief = next(doc for doc in room["docs"] if doc["name"] == "incident-brief")
    assert "abc" in brief["content"]
    assert "Why is checkout failing?" in brief["content"]
    assert "read-only" in room["instructions"].lower()
    assert "No cluster mutation" in room["instructions"]
    assert {role["name"] for role in room["roles"]} == {
        "incident-commander",
        "evidence-verifier",
    }


def test_incident_template_normalizes_whitespace_in_the_question():
    document = yaml.safe_load(
        specs.incident_room_template(
            room_name="incident-abc-shop-checkout",
            incident_id="abc",
            cluster="demo-cluster",
            namespace="shop",
            service="checkout",
            deployment="checkout",
            question="Why\n  is   checkout\tfailing?",
        )
    )
    brief = next(doc for doc in document["room"]["docs"] if doc["name"] == "incident-brief")
    assert "Why is checkout failing?" in brief["content"]
    assert "\t" not in brief["content"]


def test_incident_kickoff_is_top_level_beside_the_room():
    text = specs.incident_room_template(
        room_name="incident-abc-shop-checkout",
        incident_id="abc",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
        question="Why is checkout failing?",
        kickoff="@incident-commander Start the read-only investigation.",
    )
    document = yaml.safe_load(text)
    assert document["kickoff"].strip() == "@incident-commander Start the read-only investigation."
    assert "kickoff" not in document["room"]


def test_console_instructions_name_the_switch_tools_and_mcp_prefix():
    for spec in specs.AGENTS:
        text = specs.console_instructions(spec.name)
        assert "send_targeted_message" in text
        assert "ai-sre" in text
        assert "create_room_document" in text
        assert "read-only" in text.lower()
    assert "send_targeted_message" in specs.console_instructions("cluster-investigator")
    assert "final-report" in specs.console_instructions("incident-commander")
    assert "evidence-summary" in specs.console_instructions("cluster-investigator")
    assert "verification" in specs.console_instructions("evidence-verifier")


def test_individual_core_room_yaml_matches_the_group_definition():
    for key, name in specs.CORE_ROOMS.items():
        document = yaml.safe_load(specs.core_room_yaml(key))
        assert document["version"] == 1
        assert document["room"]["name"] == name
        assert document["room"]["agents"] == list(specs.CORE_ROOM_AGENTS[key])
        assert "bridge" not in document["room"]


def test_role_mapping_uses_switch_identity_names():
    assert specs.agent_name_for_role("coordinator") == "incident-commander"
    assert specs.agent_name_for_role("evidence-verifier") == "evidence-verifier"
    assert specs.agent_name_for_role("telemetry-analyst") == "telemetry-analyst"
    assert specs.agent_name_for_role("operator") == "operator"
