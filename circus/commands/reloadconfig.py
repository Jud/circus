from circus.commands.base import Command


class Quit(Command):
    """\
        Quit the arbiter immediately
        ============================

        When the arbiter receive this command, the arbiter reloads the
        configuration file.

        ZMQ Message
        -----------

        ::

            {
                "command": "reloadconfig"
            }

        The response return the status "ok".


        Command line
        ------------

        ::

            $ circusctl reloadconfig

    """
    name = "reloadconfig"

    def message(self, *args, **opts):
        return self.make_message()

    def execute(self, arbiter, opts):
        arbiter.reload_from_config()
