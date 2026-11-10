import bpy

from . import cli, hooks


class BLENDERHACKATIME_OT_set_key(bpy.types.Operator):
    bl_idname = "preferences.blender_hackatime_set_key"
    bl_label = "Set Hackatime API Key"
    bl_options = {"INTERNAL"}

    api_key: bpy.props.StringProperty(name="API Key", subtype="PASSWORD", options={"SKIP_SAVE"})
    api_url: bpy.props.StringProperty(name="API URL", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        config = cli.read_config()
        self.api_key = config.get("api_key", "")
        self.api_url = config.get("api_url", cli.DEFAULT_API_URL)
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        values = {"api_url": self.api_url.strip() or cli.DEFAULT_API_URL}
        key = self.api_key.strip()
        if key:
            values["api_key"] = key
        try:
            cli.write_config(values)
        except OSError as exc:
            self.report({"ERROR"}, f"Could not write {cli.config_path()}: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Saved to {cli.config_path()}")
        return {"FINISHED"}


class BlenderHackatimePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    def draw(self, context):
        layout = self.layout
        config = cli.read_config()
        sender = hooks.state.sender
        column = layout.column(align=True)
        column.label(text=f"Config: {cli.config_path()}")
        column.label(text=f"API URL: {config.get('api_url', 'not set')}")
        column.label(text="API key: set" if config.get("api_key") else "API key: not set", icon="CHECKMARK" if config.get("api_key") else "ERROR")
        found = cli.find_cli()
        column.label(text=f"wakatime-cli: {found}" if found else "wakatime-cli: not found, it will be downloaded")
        if sender is not None and sender.last_error:
            column.label(text=f"Last error: {sender.last_error}", icon="ERROR")
        layout.operator(BLENDERHACKATIME_OT_set_key.bl_idname, icon="KEYINGSET")


classes = (BLENDERHACKATIME_OT_set_key, BlenderHackatimePreferences)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
