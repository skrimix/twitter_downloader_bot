import html
import json
import logging
import traceback
from io import StringIO
from os import makedirs
from os import makedirs
from tempfile import TemporaryFile
from typing import Optional
from urllib.parse import urlsplit

import requests

try:
    import re2 as re
except ImportError:
    import re
import telegram.error
from telegram import Update, InputMediaDocument, InputMediaAnimation, constants, BotCommand, BotCommandScopeChat
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, PicklePersistence

from config import BOT_TOKEN, DEVELOPER_ID, IS_BOT_PRIVATE

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class APIException(Exception):
    pass


def extract_tweet_ids(update: Update) -> Optional[list[str]]:
    """Extract tweet IDs from message."""
    text = update.effective_message.text

    # For t.co links
    unshortened_links = ''
    for link in re.findall(r"t\.co\/[a-zA-Z0-9]+", text):
        try:
            unshortened_link = requests.get('https://' + link).url
            unshortened_links += '\n' + unshortened_link
            log_handling(update, 'info', f'Unshortened t.co link [https://{link} -> {unshortened_link}]')
        except:
            log_handling(update, 'info', f'Could not unshorten link [https://{link}]')

    # Parse IDs from received text
    tweet_ids = re.findall(r"(?:twitter|x)\.com/.{1,15}/(?:web|status(?:es)?)/([0-9]{1,20})", text + unshortened_links)
    tweet_ids = list(dict.fromkeys(tweet_ids))
    return tweet_ids or None


def scrape_media(tweet_id: int) -> list[dict]:
    r = requests.get(f'https://api.vxtwitter.com/Twitter/status/{tweet_id}')
    r.raise_for_status()
    try:
        return r.json()['media_extended']
    except requests.exceptions.JSONDecodeError: # the api likely returned an HTML page, try looking for an error message
        # <meta content="{message}" property="og:description" />
        if match := re.search(r'<meta content="(.*?)" property="og:description" />', r.text):
            raise APIException(f'API returned error: {html.unescape(match.group(1))}')
        raise
        

def reply_media(update: Update, context: CallbackContext, tweet_media: list) -> bool:
    """Reply to message with supported media."""
    photos = [media for media in tweet_media if media["type"] == "image"]
    gifs = [media for media in tweet_media if media["type"] == "gif"]
    videos = [media for media in tweet_media if media["type"] == "video"]
    if photos:
        reply_photos(update, context, photos)
    if gifs:
        reply_gifs(update, context, gifs)
    elif videos:
        reply_videos(update, context, videos)
    return bool(photos or gifs or videos)


def reply_photos(update: Update, context: CallbackContext, twitter_photos: list[dict]) -> None:
    """Reply with photo group."""
    photo_group = []
    for photo in twitter_photos:
        photo_url = photo['url']
        log_handling(update, 'info', f'Photo[{len(photo_group)}] url: {photo_url}')
        parsed_url = urlsplit(photo_url)

        # Try changing requested quality to 'orig'
        try:
            new_url = parsed_url._replace(query='format=jpg&name=orig').geturl()
            log_handling(update, 'info', 'New photo url: ' + new_url)
            requests.head(new_url).raise_for_status()
            photo_group.append(InputMediaDocument(media=new_url))
        except requests.HTTPError:
            log_handling(update, 'info', 'orig quality not available, using original url')
            photo_group.append(InputMediaDocument(media=photo_url))
    update.effective_message.reply_media_group(photo_group, quote=True)
    log_handling(update, 'info', f'Sent photo group (len {len(photo_group)})')
    context.bot_data['stats']['media_downloaded'] += len(photo_group)


def reply_gifs(update: Update, context: CallbackContext, twitter_gifs: list[dict]):
    """Reply with GIF animations."""
    for gif in twitter_gifs:
        gif_url = gif['url']
        log_handling(update, 'info', f'Gif url: {gif_url}')
        update.effective_message.reply_animation(animation=gif_url, quote=True)
        log_handling(update, 'info', 'Sent gif')
        context.bot_data['stats']['media_downloaded'] += 1


def reply_videos(update: Update, context: CallbackContext, twitter_videos: list[dict]):
    """Reply with videos."""
    for video in twitter_videos:
        video_url = video['url']
        try:
            request = requests.get(video_url, stream=True)
            request.raise_for_status()
            if (video_size := int(request.headers['Content-Length'])) <= constants.MAX_FILESIZE_DOWNLOAD:
                # Try sending by url
                update.effective_message.reply_video(video=video_url, quote=True)
                log_handling(update, 'info', 'Sent video (download)')
            elif video_size <= constants.MAX_FILESIZE_UPLOAD:
                log_handling(update, 'info', f'Video size ({video_size}) is bigger than '
                                            f'MAX_FILESIZE_UPLOAD, using upload method')
                message = update.effective_message.reply_text(
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
                    update.effective_message.reply_video(video=tf, quote=True, supports_streaming=True)
                    log_handling(update, 'info', 'Sent video (upload)')
                message.delete()
            else:
                log_handling(update, 'info', 'Video is too large, sending direct link')
                update.effective_message.reply_text(f'Video is too large for Telegram upload. Direct video link:\n'
                                        f'{video_url}', quote=True)
        except (requests.HTTPError, KeyError, telegram.error.BadRequest, requests.exceptions.ConnectionError) as exc:
            log_handling(update, 'info', f'{exc.__class__.__qualname__}: {exc}')
            log_handling(update, 'info', 'Error occurred when trying to send video, sending direct link')
            update.effective_message.reply_text(f'Error occurred when trying to send video. Direct link:\n'
                                    f'{video_url}', quote=True)
        context.bot_data['stats']['media_downloaded'] += 1


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
        # logger.critical(msg="Requests conflict found, exiting...")
        # kill(getpid(), SIGTERM)
        logger.error("Telegram requests conflict")
        return

    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    
    # if there is no update, don't send an error report (probably a network error, happens sometimes)
    if update is None:
        return

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
                              caption='#error_report\nAn exception was raised in runtime\n')

    if update:
        error_class_name = ".".join([context.error.__class__.__module__, context.error.__class__.__qualname__])
        update.effective_message.reply_text(f'Error\n{error_class_name}: {str(context.error)}')


def start(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    log_handling(update, 'info', f'Received /start command from userId {update.effective_user.id}')
    user = update.effective_user
    update.effective_message.reply_markdown_v2(
        fr'Hi {user.mention_markdown_v2()}\!' +
        '\nSend tweet link here and I will download media in the best available quality for you'
    )


def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    update.effective_message.reply_text('Send tweet link here and I will download media in the best available quality for you')


def stats_command(update: Update, context: CallbackContext) -> None:
    """Send stats when the command /stats is issued."""
    if not 'stats' in context.bot_data:
        context.bot_data['stats'] = {'messages_handled': 0, 'media_downloaded': 0}
        logger.info('Initialized stats')
    logger.info(f'Sent stats: {context.bot_data["stats"]}')
    update.effective_message.reply_markdown_v2(f'*Bot stats:*\nMessages handled: *{context.bot_data["stats"].get("messages_handled")}*'
                                     f'\nMedia downloaded: *{context.bot_data["stats"].get("media_downloaded")}*')


def reset_stats_command(update: Update, context: CallbackContext) -> None:
    """Reset stats when the command /resetstats is issued."""
    stats = {'messages_handled': 0, 'media_downloaded': 0}
    context.bot_data['stats'] = stats
    logger.info("Bot stats have been reset")
    update.effective_message.reply_text("Bot stats have been reset")


def deny_access(update: Update, context: CallbackContext) -> None:
    """Deny unauthorized access"""
    log_handling(update, 'info',
                 f'Access denied to {update.effective_user.full_name} (@{update.effective_user.username}),'
                 f' userId {update.effective_user.id}')
    update.effective_message.reply_text(f'Access denied. Your id ({update.effective_user.id}) is not whitelisted')


def handle_message(update: Update, context: CallbackContext) -> None:
    """Handle the user message. Reply with found supported media."""
    log_handling(update, 'info', 'Received message: ' + update.effective_message.text.replace("\n", ""))
    if not 'stats' in context.bot_data:
        context.bot_data['stats'] = {'messages_handled': 0, 'media_downloaded': 0}
        logger.info('Initialized stats')
    context.bot_data['stats']['messages_handled'] += 1

    if tweet_ids := extract_tweet_ids(update):
        log_handling(update, 'info', f'Found Tweet IDs {tweet_ids} in message')
    else:
        log_handling(update, 'info', 'No supported tweet link found')
        update.effective_message.reply_text('No supported tweet link found', quote=True)
        return
    found_media = False
    found_tweets = False
    for tweet_id in tweet_ids:
        # Scrape a single tweet by ID
        log_handling(update, 'info', f'Scraping tweet ID {tweet_id}')
        try:
            media = scrape_media(tweet_id)
            found_tweets = True
            if media:
                log_handling(update, 'info', f'tweet media: {media}')
                if reply_media(update, context, media):
                    found_media = True
                else:
                    log_handling(update, 'info', f'Found unsupported media: {media[0]["type"]}')
            else:
                log_handling(update, 'info', f'Tweet {tweet_id} has no media')
                update.effective_message.reply_text(f'Tweet {tweet_id} has no media', quote=True)
        except APIException as exc:
            log_handling(update, 'error', f'Error occurred when scraping tweet {tweet_id}: {traceback.format_exc()}')
            update.effective_message.reply_text(f'Error occurred when scraping tweet {tweet_id}\n{exc}', quote=True)
        except Exception:
            log_handling(update, 'error', f'Error occurred when scraping tweet {tweet_id}: {traceback.format_exc()}')
            update.effective_message.reply_text(f'Error handling tweet {tweet_id}', quote=True)
            

    if found_tweets and not found_media:
        log_handling(update, 'info', 'No supported media found')
        update.effective_message.reply_text('No supported media found', quote=True)

def handle_channel_post(update: Update, context: CallbackContext) -> None:
    log_handling(update, 'info', f'Leaving channel {update.effective_chat.id}')
    update.effective_chat.leave()


def main() -> None:
    """Start the bot."""
    makedirs('data', exist_ok=True)  # Create data
    persistence = PicklePersistence(filename='data/persistence')

    # Create the Updater and pass it your bot's token.
    updater = Updater(BOT_TOKEN, persistence=persistence)

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
        
        # on channel post - leave channel
        dispatcher.add_handler(MessageHandler(Filters.chat_type.channel, handle_channel_post))

        # on non command i.e message - handle the message
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message, run_async=True))

        # Set commands menu
        # Public commands are useless for now
        # public_commands = [BotCommand("start", "Start the bot"), BotCommand("help", "Help message")]
        public_commands = []
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


if __name__ == '__main__':
    main()
