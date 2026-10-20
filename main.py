"""Interactive command-line entry point; stage implementations live in workflow_agents/."""
import argparse
import asyncio
from workflow_agents.interfaces.cli import main

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SWMM-Agentic3 interactive workflow')
    parser.add_argument('task', nargs='?', default='List available SWMM-2D model projects under models/.')
    args = parser.parse_args()
    asyncio.run(main(args.task))
