from src.discord_bot import MASTER_EMBED_COLOUR, MASTER_EMBED_FOOTER, make_after_hours_embed
from src.preview_discord_ui import gallery_embeds


def test_shared_theme_factory_matches_master_reference():
    embed=make_after_hours_embed('𝐓𝐄𝐒𝐓','A compact preview.')
    assert embed.colour.value == MASTER_EMBED_COLOUR == 0xFF7F00
    assert embed.footer.text == MASTER_EMBED_FOOTER
    assert embed.footer.icon_url is None


def test_gallery_uses_shared_theme_and_is_safe_to_preview():
    states=gallery_embeds()
    assert len(states) >= 12
    for _group,embed in states:
        assert embed.colour.value == 0xFF7F00
        assert embed.footer.text == MASTER_EMBED_FOOTER
        text=f'{embed.title}\n{embed.description}\n' + '\n'.join(field.value for field in embed.fields)
        assert '[image](' not in text.lower() and 'svg' not in text.lower()
        assert '<@&1346328166100107366>' not in text
    cover='\n'.join(embed.description for group,embed in states if group == 'APPLICATION UI' and '𝐂𝐎𝐕𝐄𝐑 𝐋𝐄𝐓𝐓𝐄𝐑' in embed.title)
    assert 'andreicastillofficial@gmail.com' in cover
    assert 'https://github.com/draiimon' in cover
    assert 'https://www.draiimon.gt.tc/' in cover
