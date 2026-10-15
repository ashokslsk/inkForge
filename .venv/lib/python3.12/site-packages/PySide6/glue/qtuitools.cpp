// Copyright (C) 2018 The Qt Company Ltd.
// SPDX-License-Identifier: LicenseRef-Qt-Commercial OR LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
// Qt-Security score:critical reason:execute-external-code

// @snippet uitools-loadui
/*
 * Based on code provided by:
 *          Antonio Valentino <antonio.valentino at tiscali.it>
 *          Frédéric <frederic.mantegazza at gbiloba.org>
 */

#include <sbkpython.h>
#include <sbkconverter.h>

#include <pysideutils.h>

#include <QtUiTools/QUiLoader>
#include <QtWidgets/QWidget>

#include <QtCore/QFile>
#include <QtCore/QFileInfo>

static void createChildrenNameAttributes(PyObject *root, QObject *object)
{
    for (auto *child : object->children()) {
        const QByteArray name = child->objectName().toLocal8Bit();

        if (!name.isEmpty() && !name.startsWith("_") && !name.startsWith("qt_")) {
            Shiboken::AutoDecRef attrName(Py_BuildValue("s", name.constData()));
            if (!PyObject_HasAttr(root, attrName)) {
                Shiboken::AutoDecRef pyChild(%CONVERTTOPYTHON[QObject *](child));
                PyObject_SetAttr(root, attrName, pyChild);
            }
            createChildrenNameAttributes(root, child);
        }
        createChildrenNameAttributes(root, child);
    }
}

static PyObject *QUiLoadedLoadUiFromDevice(QUiLoader *self, QIODevice *dev, QWidget *parent)
{
    QWidget *wdg = self->load(dev, parent);

    if (wdg) {
        PyObject *pyWdg = %CONVERTTOPYTHON[QWidget *](wdg);
        createChildrenNameAttributes(pyWdg, wdg);
        if (parent) {
            Shiboken::AutoDecRef pyParent(%CONVERTTOPYTHON[QWidget *](parent));
            Shiboken::Object::setParent(pyParent, pyWdg);
        }
        return pyWdg;
    }

    if (!PyErr_Occurred())
        PyErr_Format(PyExc_RuntimeError, "Unable to open/read ui device");
    return nullptr;
}

static PyObject *QUiLoaderLoadUiFromFileName(QUiLoader *self, const QString &uiFile, QWidget *parent)
{
    QFile fd(uiFile);
    return QUiLoadedLoadUiFromDevice(self, &fd, parent);
}
// @snippet uitools-loadui

// @snippet quiloader
Q_IMPORT_PLUGIN(PyCustomWidgets);
// @snippet quiloader

// @snippet quiloader-registercustomwidget
registerCustomWidget(%PYARG_1);
%CPPSELF.addPluginPath(QString{}); // force reload widgets
// @snippet quiloader-registercustomwidget

// @snippet quiloader-load-1
// Avoid calling the original function: %CPPSELF.%FUNCTION_NAME()
%PYARG_0 = QUiLoadedLoadUiFromDevice(%CPPSELF, %1, %2);
// @snippet quiloader-load-1

// @snippet quiloader-load-2
// Avoid calling the original function: %CPPSELF.%FUNCTION_NAME()
auto str = PySide::pyPathToQString(%1);
%PYARG_0 = QUiLoaderLoadUiFromFileName(%CPPSELF, str, %2);
// @snippet quiloader-load-2

// @snippet loaduitype
/*
Arguments:
    %PYARG_1 (uifile)
*/
%PYARG_0 = loadUiType(%PYARG_1, %PYARG_2);
// @snippet loaduitype
