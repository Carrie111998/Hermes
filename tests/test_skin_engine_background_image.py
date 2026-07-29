from hermes_cli.skin_engine import _build_skin_config


def test_build_skin_config_preserves_background_image_fields() -> None:
    skin = _build_skin_config(
        {
            "name": "wallpaper-skin",
            "colors": {"background": "#101010"},
            "background_image": "images/wall.png",
            "background_image_fit": "contain",
            "background_image_position": "top right",
            "background_overlay": "#00000066",
        }
    )

    assert skin.background_image == "images/wall.png"
    assert skin.background_image_fit == "contain"
    assert skin.background_image_position == "top right"
    assert skin.background_overlay == "#00000066"
