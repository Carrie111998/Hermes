from hermes_cli.kanban import build_parser
import argparse

def test_create_and_assign_parse_execution_owner_contract():
    root = argparse.ArgumentParser(); subs = root.add_subparsers(dest='cmd')
    build_parser(subs)
    args = root.parse_args(['kanban','create','x','--owner-kind','external','--task-kind','ordinary','--purpose','p','--creation-authority','op'])
    assert args.owner_kind == 'external'
    assert args.task_kind == 'ordinary'
    assigned = root.parse_args(['kanban','assign','t_12345678','lane','--owner-kind','external'])
    assert assigned.owner_kind == 'external'
