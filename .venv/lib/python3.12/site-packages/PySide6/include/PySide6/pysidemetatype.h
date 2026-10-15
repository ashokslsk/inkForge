// Copyright (C) 2022 The Qt Company Ltd.
// SPDX-License-Identifier: LicenseRef-Qt-Commercial OR LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only
// Qt-Security score:significant reason:default

#ifndef PYSIDEMETATYPE_H
#define PYSIDEMETATYPE_H

#include <sbkpython.h>

#include <pysidemacros.h>

#include <QtCore/qtclasshelpermacros.h>

QT_FORWARD_DECLARE_CLASS(QMetaType)
QT_FORWARD_DECLARE_STRUCT(QMetaObject)

namespace PySide
{

/// Returns the QMetaType matching a PyTypeObject
/// \param
/// \param type TypeObject
/// \return QMetaType
PYSIDE_API QMetaType qMetaTypeFromPyType(PyTypeObject *type);

/// Create a QMetaType for a pointer to a QObject-derived Python class
/// for use in signal/slot connections (SIGNAL("mySignal(MyClass*)")).
/// \param metaObject QMetaObject
/// \return QMetaType
PYSIDE_API QMetaType createQObjectPtrMetaType(const QMetaObject *metaObject);
} //namespace PySide

#endif // PYSIDEMETATYPE_H
