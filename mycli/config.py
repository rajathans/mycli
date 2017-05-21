from __future__ import print_function
from io import BytesIO, TextIOWrapper
import logging
import os
import struct
import sys
from configobj import ConfigObj
from cli_helpers.config import Config, get_system_config_dirs
from cli_helpers.compat import WIN
try:
    basestring
    from UserDict import UserDict
except NameError:
    basestring = str
    from collections import UserDict
from Crypto.Cipher import AES

PACKAGE_ROOT = os.path.dirname(__file__)


class MySqlConfig(Config):

    sections = ['client']

    def __init__(self, *args, **kwargs):
        self.defaults_file = kwargs.pop('defaults_file', None)
        self.defaults_suffix = kwargs.pop('default_suffix', None)
        self.login_path = kwargs.pop('login_path', None)

        if self.login_path:
            self.sections.append(login_path)

        if self.defaults_suffix:
            self.sections.extend([s + self.defaults_suffix
                                  for s in self.sections])

        super(self.__class__, self).__init__(*args, **kwargs)

    def read(self):
        """Read the MySQL config."""
        errors = super(self.__class__, self).read()
        data, self.data = self.data, {}
        for section in self.sections:
            self.data.update(data[section])
        return errors

    def system_config_files(self):
        """Get a list of paths to the system's config files."""
        if WIN:
            system_dirs = get_system_config_dirs(self.app_name,
                                                 self.app_author)
            system_dirs.append(os.environ.get('WINDIR'))
            system_dirs.append('C:\\')
            return [os.path.join(dir, self.filename) for dir in system_dirs]
        else:
            return [
                os.path.join('/etc', self.filename),
                os.path.join('/etc/mysql', self.filename),
                os.path.join('/usr/local/etc', self.filename)
            ]

    def user_config_file(self):
        """Get the path to the user's config file."""
        if WIN:
            return super(self.__class__, self).user_config_file()
        else:
            return os.path.expanduser(os.path.join('~', '.{}'.format(
                self.filename)))

    def login_path_file(self):
        """Return the path to the login path file or None if it doesn't exist."""
        mylogin_cnf_path = os.environ.get('MYSQL_TEST_LOGIN_FILE')

        if mylogin_cnf_path is None:
            default_dir = '~'
            if WIN:
                default_dir = os.path.join(os.environ.get('APPDATA'), 'MySQL')

            mylogin_cnf_path = os.path.expanduser(os.path.join(
                default_dir, '.mylogin.cnf'))

        if os.path.exists(mylogin_cnf_path):
            # TODO: Check permissions.
            logger.debug("Found login path file at '{0}'".format(
                mylogin_cnf_path))
            return mylogin_cnf_path
        return None

    def all_config_files(self):
        """Get a list of all the MySQL config files.

        The login path file is returned as a decrypted TextIOWrapper.
        """
        login_path_file = self.login_path_file()

        if self.defaults_file:
            files = [self.defaults_file]
        else:
            files = (self.additional_files() + self.system_config_files() +
                     [self.user_config_file()])

        if login_path_file:
            files.append(open_mylogin_cnf(login_path_file))

        return files


class MyCliConfig(Config):

    mysql_sections = ['client']
    mysql_filename = 'my.cnf'
    filename = 'myclirc'
    additional_dirs = (
        '/etc',
    )

    def __init__(self, *args, **kwargs):
        mysql_default = kwargs.pop('mysql_default', None)
        mysql_default_suffix = kwargs.pop('mysql_default_suffix', None)
        mysql_login_path = kwargs.pop('mysql_login_path', None)

        kwargs['default'] = self.default_config_file()
        super(MyCliConfig, self).__init__(*args, **kwargs,
                                          additional_dirs=self.additional_dirs)
        # TODO: look into other MySQL server versions.
        self.mysql = MySqlConfig('MySQL Server 5.7', 'MySQL',
                                        self.filename,
                                        default=self.default_mysql_file(),
                                        validate=True)
        self.mysql.read()

    def default_mysql_file(self):
        return os.path.join(PACKAGE_ROOT, self.mysql_filename)

    def default_config_file(self):
        return os.path.join(PACKAGE_ROOT, self.filename)


logger = logging.getLogger(__name__)

def log(logger, level, message):
    """Logs message to stderr if logging isn't initialized."""

    if logger.parent.name != 'root':
        logger.log(level, message)
    else:
        print(message, file=sys.stderr)


def open_mylogin_cnf(name):
    """Open a readable version of .mylogin.cnf.

    Returns the file contents as a TextIOWrapper object.

    :param str name: The pathname of the file to be opened.
    :return: the login path file or None
    """

    try:
        with open(name, 'rb') as f:
            plaintext = read_and_decrypt_mylogin_cnf(f)
    except (OSError, IOError):
        logger.error('Unable to open login path file.')
        return None

    if not isinstance(plaintext, BytesIO):
        logger.error('Unable to read login path file.')
        return None

    return TextIOWrapper(plaintext)

def read_and_decrypt_mylogin_cnf(f):
    """Read and decrypt the contents of .mylogin.cnf.

    This decryption algorithm mimics the code in MySQL's
    mysql_config_editor.cc.

    The login key is 20-bytes of random non-printable ASCII.
    It is written to the actual login path file. It is used
    to generate the real key used in the AES cipher.

    :param f: an I/O object opened in binary mode
    :return: the decrypted login path file
    :rtype: io.BytesIO or None
    """

    # Number of bytes used to store the length of ciphertext.
    MAX_CIPHER_STORE_LEN = 4

    LOGIN_KEY_LEN = 20

    # Move past the unused buffer.
    buf = f.read(4)

    if not buf or len(buf) != 4:
        logger.error('Login path file is blank or incomplete.')
        return None

    # Read the login key.
    key = f.read(LOGIN_KEY_LEN)

    # Generate the real key.
    rkey = [0] * 16
    for i in range(LOGIN_KEY_LEN):
        try:
            rkey[i % 16] ^= ord(key[i:i+1])
        except TypeError:
            # ord() was unable to get the value of the byte.
            logger.error('Unable to generate login path AES key.')
            return None
    rkey = struct.pack('16B', *rkey)

    # Create a cipher object using the key.
    aes_cipher = AES.new(rkey, AES.MODE_ECB)

    # Create a bytes buffer to hold the plaintext.
    plaintext = BytesIO()

    while True:
        # Read the length of the ciphertext.
        len_buf = f.read(MAX_CIPHER_STORE_LEN)
        if len(len_buf) < MAX_CIPHER_STORE_LEN:
            break
        cipher_len, = struct.unpack("<i", len_buf)

        # Read cipher_len bytes from the file and decrypt.
        cipher = f.read(cipher_len)
        pplain = aes_cipher.decrypt(cipher)

        try:
            # Determine pad length.
            pad_len = ord(pplain[-1:])
        except TypeError:
            # ord() was unable to get the value of the byte.
            logger.warning('Unable to remove pad.')
            continue

        if pad_len > len(pplain) or len(set(pplain[-pad_len:])) != 1:
            # Pad length should be less than or equal to the length of the
            # plaintext. The pad should have a single unqiue byte.
            logger.warning('Invalid pad found in login path file.')
            continue

        # Get rid of pad.
        plain = pplain[:-pad_len]
        plaintext.write(plain)

    if plaintext.tell() == 0:
        logger.error('No data successfully decrypted from login path file.')
        return None

    plaintext.seek(0)
    return plaintext
