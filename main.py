import html
import json
import logging
import traceback
from urllib.parse import urlsplit
from io import StringIO

try:
    import re2 as re
except ImportError:
    import re
import snscrape.modules.twitter as sntwitter
import telegram.error
from telegram import Update, InputMediaDocument, error, ParseMode, constants
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

BOT_TOKEN = "${{ secrets.bot_token }}"  # Your telegram bot token
DEVELOPER_ID = int("${{ secrets.developer_id }}")  # Your telegram id (used for error reporting and private mode)
IS_BOT_PRIVATE = False  # Change this to True to make bot private

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Compile regex for searching tweet ID in messages
r = re.compile(r"twitter\.com\/.*\/status(?:es)?\/([^\/\?]+)")


def log_handling_info(update: Update, message) -> None:
    logger.info(f'[{update.effective_chat.id}:{update.effective_message.message_id}] {message}')


def log_handling_error(update: Update, message) -> None:
    logger.error(f'[{update.effective_chat.id}:{update.effective_message.message_id}] {message}')


def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""

    if type(context.error) == telegram.error.Unauthorized:
        return

    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'An exception was raised during runtime\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    # Finally, send the message
    # If message is too long, send it as document
    if len(message) <= constants.MAX_MESSAGE_LENGTH:
        context.bot.send_message(chat_id=DEVELOPER_ID, text=message, parse_mode=ParseMode.HTML)
    else:
        logger.warning('Error message is too long, sending as file')
        message = (
            f'update = {json.dumps(update_str, indent=2, ensure_ascii=False)}'
            '\n\n'
            f'context.chat_data = {str(context.chat_data)}\n\n'
            f'context.user_data = {str(context.user_data)}\n\n'
            f'{tb_string}'
        )
        string_out = StringIO(message)
        context.bot.send_document(chat_id=DEVELOPER_ID, document=string_out, filename='error.txt',
                                  caption='An exception was raised during runtime\n')

    if update:
        update.effective_message.reply_text(f'Error\n{context.error.__class__.__name__}: {str(context.error)}')


def start(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    log_handling_info(update, f'Received /start command from userId {update.effective_user.id}')
    user = update.effective_user
    update.message.reply_markdown_v2(
        fr'Hi {user.mention_markdown_v2()}\!' +
        '\nSend tweet URL here and I will download original quality images from that tweet for you'
    )


def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    update.message.reply_text('Send tweet URL here and I will download original quality images from that tweet for you')


def deny_access(update: Update, context: CallbackContext) -> None:
    """Deny unauthorized access"""
    log_handling_info(update, f'Access denied to {update.effective_user.full_name} (@{update.effective_user.username}),'
                              f' userId {update.effective_user.id}')
    update.message.reply_text(f'Access denied. Your id ({update.effective_user.id}) is not in whitelist')


def handle_message(update: Update, context: CallbackContext) -> None:
    """Handle the user message. Reply with found supported media"""
    log_handling_info(update, 'Received message: ' + update.message.text.replace("\n", ""))

    # Search for tweet ID in received message
    m = r.search(update.message.text)
    if m:
        tweet_id = m.group(1)
        log_handling_info(update, 'Found url to Tweet ID ' + tweet_id)
    else:
        log_handling_info(update, 'No valid tweet url found')
        update.message.reply_text('No valid tweet url found', quote=True)
        return

    # Scrape a single tweet by ID
    tweet = sntwitter.TwitterTweetScraper(tweet_id, sntwitter.TwitterTweetScraperMode.SINGLE).get_items().__next__()
    media_group = []
    for twitter_media in tweet.media:
        # We support only photos (for now)
        if type(twitter_media) == sntwitter.Photo:
            log_handling_info(update, f'Photo[{len(media_group)}] url: {twitter_media.fullUrl}')
            parsed_url = urlsplit(twitter_media.fullUrl)

            # Change requested quality to 'orig'
            new_url = parsed_url._replace(query='format=jpg&name=orig').geturl()
            log_handling_info(update, 'New photo url: ' + new_url)

            media_group.append(InputMediaDocument(media=new_url))
        else:
            log_handling_info(update, f'Ignoring unsupported media: {type(twitter_media)}')

    # Check if we have found any media to send
    if media_group:
        try:
            update.message.reply_media_group(media_group, quote=True)
            log_handling_info(update, f'Sent media group (len {len(media_group)})')
        except error.TelegramError as e:
            log_handling_error(update, 'Error occurred while sending media:\n' + e.message)
            update.message.reply_text('Error:\n' + e.message)
    else:
        log_handling_info(update, 'No supported media found')
        update.message.reply_text('No supported media found', quote=True)


def main() -> None:
    """Start the bot."""
    # Create the Updater and pass it your bot's token.
    updater = Updater(BOT_TOKEN)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    if IS_BOT_PRIVATE:
        # Deny access to everyone but developer
        dispatcher.add_handler(MessageHandler(~Filters.chat(DEVELOPER_ID), deny_access))

        # on different commands - answer in Telegram
        dispatcher.add_handler(CommandHandler("start", start, Filters.chat(DEVELOPER_ID)))
        dispatcher.add_handler(CommandHandler("help", help_command, Filters.chat(DEVELOPER_ID)))

        # on non command i.e message - echo the message on Telegram
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command & Filters.chat(DEVELOPER_ID),
                                              handle_message, run_async=True))

    else:
        # on different commands - answer in Telegram
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("help", help_command))

        # on non command i.e message - echo the message on Telegram
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message, run_async=True))

    dispatcher.add_error_handler(error_handler)

    # Start the Bot
    updater.start_polling()

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()


if __name__ == '__main__':
    main()
