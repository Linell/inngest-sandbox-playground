import flask
import inngest
import inngest.flask

client = inngest.Inngest(app_id="sandbox-playground", is_production=False)


@client.create_function(
    fn_id="hello",
    trigger=inngest.TriggerEvent(event="playground/hello"),
)
def hello(ctx: inngest.ContextSync) -> str:
    with ctx.group.sandbox("hello") as sandbox:
        result = sandbox.commands.run("say-hello", "echo 'Hello, World!'")
    return result.stdout


app = flask.Flask(__name__)
inngest.flask.serve(app, client, [hello])

if __name__ == "__main__":
    app.run(port=6967)
