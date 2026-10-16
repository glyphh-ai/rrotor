"""The worker: the one process in a run's sandbox. It locks itself down (Landlock), opens the proxy's door
(127.0.0.1:3128 in its network namespace), imports the model and runs `load`, the function and `end`, talking to
the runner over fd 3. Standard library only: it runs on the model's own Python."""
