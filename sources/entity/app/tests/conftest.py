import os, sys
# make `import config, tools, agent, ...` work from the port dir
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
