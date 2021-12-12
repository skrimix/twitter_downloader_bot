import html
import json
import logging
import traceback
from io import StringIO
from os import getpid, kill
from signal import SIGTERM
from tempfile import TemporaryFile
from urllib.parse import urlsplit

import requests

try:
    import re2 as re
except ImportError:
    import re
import snscrape.base
import snscrape.modules.twitter as sntwitter
import telegram.error
from telegram import Update, InputMediaDocument, constants, BotCommand, BotCommandScopeChat
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

from config import BOT_TOKEN, DEVELOPER_ID, IS_BOT_PRIVATE


# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# Initialize statistics
# TODO: add user stats and use PicklePersistence
try:
    with open('stats.json', 'r+', encoding="utf8") as stats_file:
        stats = json.load(stats_file)
except (FileNotFoundError, json.decoder.JSONDecodeError):
    stats = {'messages_handled': 0, 'media_downloaded': 0}


def extract_tweet_ids(text: str) -> list[str] | None:
    """Extract tweet IDs from message."""
    # Search for tweet IDs in received message
    # TODO: support t.co links
    tweet_ids = re.findall(r"twitter\.com/.*/status(?:es)?/([0-9]{1,20})", text) + \
                re.findall(r"twitter\.com/.*/web/([0-9]{1,20})", text)
    tweet_ids = list(dict.fromkeys(tweet_ids))
    return tweet_ids or None


def reply_media(update: Update, tweet_media: list) -> bool:
    """Reply to message with supported media."""
    if isinstance(tweet_media[0], sntwitter.Photo):
        reply_photos(update, tweet_media)
    elif isinstance(tweet_media[0], sntwitter.Gif):
        reply_gif(update, tweet_media[0])
    elif isinstance(tweet_media[0], sntwitter.Video):
        reply_video(update, tweet_media[0])
    else:
        return False
    return True


def reply_photos(update: Update, twitter_photos: list) -> None:
    """Reply with photo group."""
    photo_group = []
    for photo in twitter_photos:
        log_handling(update, 'info', f'Photo[{len(photo_group)}] url: {photo.fullUrl}')
        parsed_url = urlsplit(photo.fullUrl)

        # Try changing requested quality to 'orig'
        try:
            new_url = parsed_url._replace(query='format=jpg&name=orig').geturl()
            log_handling(update, 'info', 'New photo url: ' + new_url)
            requests.head(new_url).raise_for_status()
            photo_group.append(InputMediaDocument(media=new_url))
        except requests.HTTPError:
            log_handling(update, 'info', 'orig quality not available, using original url')
            photo_group.append(InputMediaDocument(media=photo.fullUrl))
    update.message.reply_media_group(photo_group, quote=True)
    log_handling(update, 'info', f'Sent photo group (len {len(photo_group)})')
    stats['media_downloaded'] += len(photo_group)


def reply_gif(update: Update, twitter_gif: sntwitter.Gif):
    """Reply with GIF animation."""
    gif_url = twitter_gif.variants[0].url
    log_handling(update, 'info', f'Gif url: {gif_url}')
    update.message.reply_animation(animation=gif_url, quote=True)
    log_handling(update, 'info', 'Sent gif')
    stats['media_downloaded'] += 1


def reply_video(update: Update, twitter_video: sntwitter.Video):
    """Reply with video."""
    # Find video variant with the best bitrate
    video = max((video_variant for video_variant in twitter_video.variants
                 if video_variant.contentType == 'video/mp4'), key=lambda x: x.bitrate)
    log_handling(update, 'info', 'Selected video variant: ' + str(video))
    try:
        request = requests.get(video.url, stream=True)
        request.raise_for_status()
        if (video_size := int(request.headers['content-length'])) <= constants.MAX_FILESIZE_DOWNLOAD:
            # Try sending by url
            update.message.reply_video(video=video.url, quote=True)
            log_handling(update, 'info', 'Sent video (download)')
        elif video_size <= constants.MAX_FILESIZE_UPLOAD:
            log_handling(update, 'info', f'Video size ({video_size}) is bigger than '
                                         f'MAX_FILESIZE_UPLOAD, using upload method')
            message = update.message.reply_text(
                'Video is too large for direct download\nUsing upload method '
                '(this might take a bit longer)',
                quote=True)
            with TemporaryFile() as tf:
                log_handling(update, 'info', f'Downloading video (Content-length: '
                                             f'{request.headers["Content-length"]})')
                for chunk in request.iter_content(chunk_size=128):
                    tf.write(chunk)
                log_handling(update, 'info', 'Video downloaded, uploading to Telegram')
                tf.seek(0)
                update.message.reply_video(video=tf, quote=True, supports_streaming=True)
                log_handling(update, 'info', 'Sent video (upload)')
            message.delete()
        else:
            log_handling(update, 'info', 'Video is too large, sending direct link')
            update.message.reply_text(f'Video is too large for Telegram upload. Direct video link:\n'
                                      f'{video.url}', quote=True)
    except (requests.HTTPError, KeyError, telegram.error.BadRequest):
        log_handling(update, 'info', 'Error occurred when trying to send video, sending direct link')
        update.message.reply_text(f'Error occurred when trying to send video. Direct video link:\n'
                                  f'{video.url}', quote=True)
    stats['media_downloaded'] += 1


# TODO: use LoggerAdapter instead
def log_handling(update: Update, level: str, message: str) -> None:
    """Log message with chat_id and message_id."""
    _level = getattr(logging, level.upper())
    logger.log(_level, f'[{update.effective_chat.id}:{update.effective_message.message_id}] {message}')


def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""

    if isinstance(context.error, telegram.error.Unauthorized):
        return

    if isinstance(context.error, telegram.error.Conflict):
        logger.critical(msg="Requests conflict found, exiting...")
        kill(getpid(), SIGTERM)

    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'#error_report\n'
        f'An exception was raised in runtime\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # Finally, send the message
    logger.info('Sending error report')
    message = (
        f'update = {json.dumps(update_str, indent=2, ensure_ascii=False)}'
        '\n\n'
        f'context.chat_data = {str(context.chat_data)}\n\n'
        f'context.user_data = {str(context.user_data)}\n\n'
        f'{tb_string}'
    )
    string_out = StringIO(message)
    context.bot.send_document(chat_id=DEVELOPER_ID, document=string_out, filename='error_report.txt',
                              caption='#error_report\nAn exception was raised during runtime\n')

    if update:
        error_class_name = ".".join([context.error.__class__.__module__, context.error.__class__.__qualname__])
        update.effective_message.reply_text(f'Error\n{error_class_name}: {str(context.error)}')


def start(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    log_handling(update, 'info', f'Received /start command from userId {update.effective_user.id}')
    user = update.effective_user
    update.message.reply_markdown_v2(
        fr'Hi {user.mention_markdown_v2()}\!' +
        '\nSend tweet link here and I will download media in best available quality for you'
    )


def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    update.message.reply_text('Send tweet link here and I will download media in best available quality for you')


def stats_command(update: Update, context: CallbackContext) -> None:
    """Send stats when the command /stats is issued."""
    logger.info(f'Sent stats: {stats}')
    update.message.reply_markdown_v2(f'*Bot stats:*\nMessages handled: *{stats.get("messages_handled")}*'
                                     f'\nMedia downloaded: *{stats.get("media_downloaded")}*')


def reset_stats_command(update: Update, context: CallbackContext) -> None:
    """Reset stats when the command /resetstats is issued."""
    global stats
    stats = {'messages_handled': 0, 'media_downloaded': 0}
    write_stats()
    logger.info("Bot stats have been reset")
    update.message.reply_text("Bot stats have been reset")


def deny_access(update: Update, context: CallbackContext) -> None:
    """Deny unauthorized access"""
    log_handling(update, 'info',
                 f'Access denied to {update.effective_user.full_name} (@{update.effective_user.username}),'
                 f' userId {update.effective_user.id}')
    update.message.reply_text(f'Access denied. Your id ({update.effective_user.id}) is not whitelisted')


def handle_message(update: Update, context: CallbackContext) -> None:
    """Handle the user message. Reply with found supported media."""
    log_handling(update, 'info', 'Received message: ' + update.message.text.replace("\n", ""))
    stats['messages_handled'] += 1

    if tweet_ids := extract_tweet_ids(update.message.text):
        log_handling(update, 'info', f'Found Tweet IDs {tweet_ids} in message')
    else:
        log_handling(update, 'info', 'No supported tweet link found')
        update.message.reply_text('No supported tweet link found', quote=True)
        return
    found_media = False
    for tweet_id in tweet_ids:
        # Scrape a single tweet by ID
        log_handling(update, 'info', f'Scraping tweet ID {tweet_id}')
        tweet = None
        try:
            tweet_scraper = sntwitter.TwitterTweetScraper(tweet_id, sntwitter.TwitterTweetScraperMode.SINGLE)
            tweet_scraper._retries = 2
            tweet = tweet_scraper.get_items().__next__()
        except (snscrape.base.ScraperException, KeyError) as exc:
            error_class_name = ".".join([exc.__class__.__module__, exc.__class__.__qualname__])
            log_handling(update, 'warning', f'Scraper exception {error_class_name}: {str(exc)}')
            update.effective_message.reply_text('Scraper error (is tweet available?)')
            return
        if tweet and tweet.media:
            log_handling(update, 'debug', f'tweet.media: {tweet.media}')
            if reply_media(update, tweet.media):
                found_media = True
            else:
                log_handling(update, 'info', f'Found unsupported media: {tweet.media[0].__class__.__name__}')

    if not found_media:
        log_handling(update, 'info', 'No supported media found')
        update.message.reply_text('No supported media found', quote=True)


def write_stats() -> None:
    """Write bot statistics to a file."""
    with open('stats.json', 'w+', encoding="utf8") as _stats_file:
        json.dump(stats, _stats_file)


def main() -> None:
    """Start the bot."""
    # Create the Updater and pass it your bot's token.
    updater = Updater(BOT_TOKEN)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # Get the bot to set commands menu
    bot = dispatcher.bot

    dispatcher.add_handler(CommandHandler("stats", stats_command, Filters.chat(DEVELOPER_ID)))
    dispatcher.add_handler(CommandHandler("resetstats", reset_stats_command, Filters.chat(DEVELOPER_ID)))

    if IS_BOT_PRIVATE:
        # Deny access to everyone but developer
        dispatcher.add_handler(MessageHandler(~Filters.chat(DEVELOPER_ID), deny_access))

        # on different commands - answer in Telegram
        dispatcher.add_handler(CommandHandler("start", start, Filters.chat(DEVELOPER_ID)))
        dispatcher.add_handler(CommandHandler("help", help_command, Filters.chat(DEVELOPER_ID)))

        # on non command i.e message - handle the message
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command & Filters.chat(DEVELOPER_ID),
                                              handle_message, run_async=True))

        # Set commands menu
        commands = [BotCommand("start", "Start the bot"), BotCommand("help", "Help message"),
                    BotCommand("stats", "Get bot statistics"), BotCommand("resetstats", "Reset bot statistics")]
        try:
            bot.set_my_commands(commands, scope=BotCommandScopeChat(DEVELOPER_ID))
        except telegram.error.BadRequest as exc:
            logger.warning(f"Couldn't set my commands for developer chat: {exc.message}")

    else:
        # on different commands - answer in Telegram
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("help", help_command))

        # on non command i.e message - handle the message
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message, run_async=True))

        # Set commands menu
        public_commands = [BotCommand("start", "Start the bot"), BotCommand("help", "Help message")]
        dev_commands = public_commands + [BotCommand("stats", "Get bot statistics"),
                                          BotCommand("resetstats", "Reset bot statistics")]
        bot.set_my_commands(public_commands)
        try:
            bot.set_my_commands(dev_commands, scope=BotCommandScopeChat(DEVELOPER_ID))
        except telegram.error.BadRequest as exc:
            logger.warning(f"Couldn't set my commands for developer chat: {exc.message}")

    dispatcher.add_error_handler(error_handler)

    # Start the Bot
    updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()

    # Write bot statistics to a file
    write_stats()


if __name__ == '__main__':
    main()
