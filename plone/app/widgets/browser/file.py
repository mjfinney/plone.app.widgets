from Acquisition import aq_parent, aq_inner
from Products.CMFCore.utils import getToolByName
from Products.Five.browser import BrowserView
from plone.app.widgets.interfaces import IATCTFileFactory
from plone.app.widgets.interfaces import IDXFileFactory
from plone.uuid.interfaces import IUUID
import json
import logging
import mimetypes
import os
import pkg_resources
import transaction

try:
    pkg_resources.get_distribution('plone.dexterity')
except pkg_resources.DistributionNotFound:
    HAS_DEXTERITY = False
else:
    from plone.dexterity.interfaces import IDexterityFTI
    HAS_DEXTERITY = True

logger = logging.getLogger('plone')


def _bool(val):
    if val.lower() in ('t', 'true', '1', 'on'):
        return True
    return False


def _tus_int(val):
    try:
        return int(val)
    except:
        return 60 * 60  # default here...


possible_tus_options = {
    'tmp_file_dir': str,
    'send_file': _bool,
    'upload_valid_duration': _tus_int
}

TUS_ENABLED = False
if os.environ.get('TUS_ENABLED'):
    try:
        from tus import Tus, Zope2RequestAdapter
        tus_settings = {}
        for option, converter in possible_tus_options.items():
            name = 'TUS_%s' % option.upper()
            if name in os.environ:
                tus_settings[option] = converter(os.environ[name])

            tmp_file_dir = tus_settings.get('tmp_file_dir')
            if tmp_file_dir is None:
                logger.warn('You are trying to enable tus but no'
                            'TUS_TMP_FILE_DIR environment setting is set.')
            elif not os.path.exists(tmp_file_dir) or \
                    not os.path.isdir(tmp_file_dir):
                logger.warn('The TUS_TMP_FILE_DIR does not point to a valid '
                            'directory.')
            elif not os.access(tmp_file_dir, os.W_OK):
                logger.warn('The TUS_TMP_FILE_DIR is not writable')
            else:
                TUS_ENABLED = True
                logger.info('tus file upload support is successfully '
                            'configured')
    except ImportError:
        logger.warn('TUS_ENABLED is set; however, tus python package is '
                    'not installed')
else:
    try:
        import tus
        tus  # pyflakes
    except ImportError:
        pass
    else:
        logger.warn('You have the tus python package installed but it is '
                    'not configured for this plone client')


class FileUploadView(BrowserView):
    """
    Handle file uploads with potential
    special handling of TUS resumable uploads
    """

    tus_uid = None

    def __contains__(self, uid):
        return self.tus_uid and self.tus_uid == uid

    def __getitem__(self, uid):
        if self.tus_uid is None:
            self.tus_uid = uid
            self.__doc__ = 'foobar'  # why is this necessary?
            return self
        else:
            raise KeyError

    def __call__(self):
        req = self.request
        tusrequest = False
        if TUS_ENABLED:
            adapter = Zope2RequestAdapter(req)
            tus = Tus(adapter, **tus_settings)
            if tus.valid:
                tusrequest = True
                tus.handle()
                if not tus.upload_finished:
                    return
                else:
                    filename = req.getHeader('FILENAME')
                    if tus.send_file:
                        filedata = req._file
                        filedata.filename = filename
                    else:
                        filepath = req._file.read()
                        filedata = open(filepath)
        if not tusrequest:
            if req.REQUEST_METHOD != 'POST':
                return
            filedata = self.request.form.get("file", None)
            if filedata is None:
                return
            filename = filedata.filename
        content_type = mimetypes.guess_type(filename)[0] or ""

        if not filedata:
            return

        # Determine if the default file/image types are DX or AT based
        ctr = getToolByName(self.context, 'content_type_registry')
        type_ = ctr.findTypeName(filename.lower(), '', '') or 'File'

        # Find or create contextual `images` folder
        context = self.context
        if context.getId() != 'images':
            parent = aq_parent(aq_inner(context))
            wtool = getToolByName(self.context, 'portal_workflow')
            for item in (context, parent):
                if 'images' in item:
                    context = item.images
                    break
                # No images folder yet; try to create one
                try:
                    item.invokeFactory('Folder', 'images')
                except ValueError:
                    # Not allowed to create folder here; try in parent
                    continue
                else:
                    # Publish images folder
                    try:
                        wtool.doActionFor(item.images, 'publish')
                    except:
                        pass
                    context = item.images
                    # The factory below commits a transaction (grr),
                    # so make sure this work isn't lost.
                    transaction.commit()
                    break
            else:
                # If unable to create images folder in context
                # or its parent, fall back to normal Plone location
                context = self.context

        DX_BASED = False
        if HAS_DEXTERITY:
            pt = getToolByName(self.context, 'portal_types')
            if IDexterityFTI.providedBy(getattr(pt, type_)):
                factory = IDXFileFactory(context)
                DX_BASED = True
            else:
                factory = IATCTFileFactory(context)
        else:
            factory = IATCTFileFactory(context)

        obj = factory(filename, content_type, filedata)

        if DX_BASED:
            if 'File' in obj.portal_type:
                size = obj.file.getSize()
                content_type = obj.file.contentType
            elif 'Image' in obj.portal_type:
                size = obj.image.getSize()
                content_type = obj.image.contentType

            result = {
                "type": content_type,
                "size": size
            }
        else:
            try:
                size = obj.getSize()
            except AttributeError:
                size = obj.getObjSize()

            result = {
                "type": obj.getContentType(),
                "size": size
            }

        if tusrequest:
            tus.cleanup_file()
        result.update({
            'url': obj.absolute_url(),
            'name': obj.getId(),
            'UID': IUUID(obj),
            'filename': filename
        })
        self.request.response.setHeader('Content-Type', 'application/json')
        return json.dumps(result)
