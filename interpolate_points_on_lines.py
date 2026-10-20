"""
***************************************************************************
*                                                                         *
*   This program is free software; you can redistribute it and/or modify  *
*   it under the terms of the GNU General Public License as published by  *
*   the Free Software Foundation; either version 2 of the License, or     *
*   (at your option) any later version.                                   *
*   Created by: Stephan "preinzi" Preinstorfer (LiberGIS)                 *
*   Last update: 20.10.2026                                               *
*   SPDX-License-Identifier: GPL-2.0-or-later                             *
*                                                                         *
***************************************************************************
"""

import bisect
import math

from qgis.PyQt.QtCore import QCoreApplication, QMetaType, QVariant
from qgis.core import (Qgis,
                       QgsCoordinateTransform,
                       QgsDistanceArea,
                       QgsExpression,
                       QgsProcessing,
                       QgsProcessingException,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterFeatureSink,
                       QgsFeature,
                       QgsFeatureRequest,
                       QgsFeatureSink,
                       QgsField,
                       QgsFields,
                       QgsGeometry,
                       QgsPoint,
                       QgsLineString,
                       QgsUnitTypes,
                       QgsVariantUtils,
                       QgsWkbTypes)


# Field names the point and segment outputs create themselves. If the
# chosen Line ID field has one of these names, it is renamed in those
# outputs ("line_<name>"). "fid" is included because a GeoPackage output
# uses a field named "fid" as its primary key: with several points or
# segments per line, those values repeat and every repeated insert fails.
RESERVED_OUTPUT_FIELDS = ('fid', 'station', 'measure', 'point_type')

# Stations closer together than this (in map units) are treated as the
# same position along the line.
STATION_EPSILON = 1e-6

# Deviation (in percent) between planar station lengths and the
# ellipsoidal ground distance above which a warning is raised.
GROUND_DEVIATION_WARN_PERCENT = 1.0

# Resolved once here instead of inside the write loops: under PyQt5 the
# fully qualified enum lookup (QgsFeatureSink.Flag.FastInsert) is about 30
# times slower than a plain attribute, which adds up over hundreds of
# thousands of written points and segments.
FAST_INSERT = QgsFeatureSink.Flag.FastInsert


def _numeric_key(value):
    """
    Normalizes an ID value for matching purposes so that, e.g., an Int
    field and a Double field carrying the same underlying ID still match
    as the same line. Falls back to the raw value if it isn't numeric.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _make_field(name, metatype_name, variant_name):
    """
    Constructs a QgsField using QMetaType.Type - the constructor argument
    QGIS >= 3.38 expects, and the form Qt6 / QGIS 4.0 is expected to keep
    long-term. QGIS versions before 3.38 (still in active use, e.g.
    3.34.x) only have the older QVariant.Type-based constructor and raise
    a TypeError for a QMetaType.Type argument, so fall back to that
    instead of relying on a QGIS version check.
    """
    try:
        return QgsField(name, getattr(QMetaType.Type, metatype_name))
    except TypeError:
        return QgsField(name, getattr(QVariant, variant_name))


def _fmt(value):
    """
    Formats a measurement value for log messages with 6 significant
    digits (0.10557027229333471 -> 0.10557) so long float values stay
    readable. Anything that isn't a number is shown as it is.
    """
    try:
        return format(value, '.6g')
    except (TypeError, ValueError):
        return str(value)


class CreateMValueLinesAlgorithm(QgsProcessingAlgorithm):
    """
    This algorithm creates LineStringZM geometries that keep the exact shape
    of the input lines. Each measurement point is projected (snapped) onto
    its line and inserted as an extra vertex at the correct position along
    the line. Z-values at the measurement points come from the chosen point
    field; Z-values at the line's own vertices are linearly interpolated
    between the surrounding measurement points. M-values represent the
    station (distance along the line) everywhere.

    A second layer is also produced: points spaced at a regular
    interval along each output line, carrying Z and M both in their
    geometry and as regular attribute fields. Where an input measurement
    point falls within half an interval of a regularly spaced position,
    that measurement point is used instead of generating a new point
    there. The start and end of each line are always kept.

    A third output layer represents the main result of this tool. The lines
    are segmented at the location of the previously generated points in
    order to create individual line geometries, which inherit the attributes
    of the points.

    All Processing algorithms should extend the QgsProcessingAlgorithm
    class.

    Most of this script was generated with claude.ai.
    """

    # Constants used to refer to parameters and outputs.

    LINE_LAYER = 'LINE_LAYER'
    MEASUREMENT_POINTS = 'MEASUREMENT_POINTS'
    LINE_ID_FIELD = 'LINE_ID_FIELD'
    POINT_ID_FIELD = 'POINT_ID_FIELD'
    Z_VALUE_FIELD = 'Z_VALUE_FIELD'
    TOLERANCE = 'TOLERANCE'
    INTERVAL = 'INTERVAL'
    OUTPUT = 'OUTPUT'
    POINT_OUTPUT = 'POINT_OUTPUT'
    SEGMENT_OUTPUT = 'SEGMENT_OUTPUT'

    def tr(self, string):
        """
        Returns a translatable string with the self.tr() function.
        """
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return CreateMValueLinesAlgorithm()

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm.
        """
        return 'interpolatepointsonlines'

    def displayName(self):
        """
        Returns the translated algorithm name (user visible).
        """
        return self.tr('Interpolate Points on Lines')

    def group(self):
        """
        Returns the name of the group this algorithm belongs to.
        """
        return self.tr('LiberGIS')

    def groupId(self):
        """
        Returns the unique ID of the group this algorithm belongs to.
        """
        return 'libergis'

    def tags(self):
        """
        Returns a list of tags relating to the algorithm, used to improve
        searchability in the Processing Toolbox.
        """
        return self.tr(
            'interpolate,points,line,lines,station,chainage,profile,z,m,zm,'
            'pipeline,road,river,measurement,survey,vertex,vertices'
        ).split(',')

    def helpUrl(self):
        """
        Returns a url pointing to a resource describing the algorithm.
        """
        return 'https://github.com/preinzi/qgis-interpolate-points-on-lines/blob/main/README.md'

    def shortHelpString(self):
        """
        Returns a localised short helper string for the algorithm.
        """
        return self.tr(
            'Creates LineStringZM geometries that follow the exact geometry of the '
            'input lines. Measurement points are snapped onto the line and inserted '
            'as extra vertices; Z-values at the measurement points come from the '
            'selected point field, Z-values at the line\'s own vertices are linearly '
            'interpolated between the surrounding measurement points, and M-values '
            'represent the station (distance along the line).\n\n'
            'A second output of regularly spaced points along each line can also be '
            'produced. Where an input measurement point already falls within half '
            'an interval of a generated position, that measurement point is reused '
            'instead of creating a redundant nearby point. The start and end of '
            'each line are always included, so the segments cover the whole line.\n\n'
            'A third output layer represents the main result of this tool. The lines '
            'are segmented at the location of the previously generated points in '
            'order to create individual line geometries, which inherit the attributes '
            'of the points.\n\n'
            'If no input points are found for a line, this line will be skipped. If '
            'exactly one point is found, that single value is applied to the whole '
            'line, as if it had been interpolated from just one measurement.\n\n'
            'The line layer must use a projected CRS: stations, tolerance and '
            'interval are measured in its map units (e.g. meters). Geographic CRS '
            '(degrees) are rejected. The log reports how far the planar stations '
            'deviate from the true ground distance on the ellipsoid.\n\n'
            'Inputs:\n'
            '- Line layer: Line features with unique IDs\n'
            '- Measurement points: Point features with line ID and numeric values\n'
            '- Line ID field: Field containing unique IDs to identify the lines '
            '(named "line_<name>" in the point/segment outputs if it is called '
            'fid, station, measure or point_type)\n'
            '- Point ID field: Field containing IDs which match the points to the lines\n'
            '- Z-value field: Field (in points layer) containing measurement information\n'
            '- Tolerance: Maximum distance a point '
            'may sit from the line before a warning is raised\n'
            '- Point interval: Spacing between the '
            'generated points along the output line\n\n'
            'Outputs:\n'
            '- Output line segments: Line layer with one individual line feature '
            'per segment between two consecutive output points (measurement or '
            'generated), carrying the "station" / "measure" / "point_type" '
            'attributes of the segment\'s first point.\n'
            '- Output lines: LineStringZM layer that keeps the original line '
            'vertices plus the measurement points, with Z = measurement value '
            '(interpolated where needed) and M = distance along line. All '
            'attributes of the input lines are kept.\n'
            '- Output points: Point layer with points every "point interval" map '
            'units along each output line (measurement points are reused where '
            'close enough instead of adding a new point), carrying Z and M in the '
            'geometry and duplicated in the "station" / "measure" attribute fields, '
            'plus the line ID and a point_type field (measurement vs generated).'
        )

    def initAlgorithm(self, config=None):
        """
        Here we define the inputs and output of the algorithm.
        """
        # We add the line layer (line vector features). Using a
        # FeatureSource parameter gives the algorithm dialog an
        # automatic "Selected features only" checkbox for this input.
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.LINE_LAYER,
                self.tr('Line layer'),
                [QgsProcessing.SourceType.TypeVectorLine]
            )
        )

        # We add the measurement points layer (point vector features).
        # FeatureSource also gives an "Selected features only" checkbox.
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.MEASUREMENT_POINTS,
                self.tr('Measurement points layer'),
                [QgsProcessing.SourceType.TypeVectorPoint]
            )
        )

        # We add the line ID field from the line layer
        self.addParameter(
            QgsProcessingParameterField(
                self.LINE_ID_FIELD,
                self.tr('Line ID field'),
                parentLayerParameterName=self.LINE_LAYER,
                type=QgsProcessingParameterField.DataType.Any
            )
        )

        # We add the point ID field from the measurement points layer
        self.addParameter(
            QgsProcessingParameterField(
                self.POINT_ID_FIELD,
                self.tr('Point ID field'),
                parentLayerParameterName=self.MEASUREMENT_POINTS,
                type=QgsProcessingParameterField.DataType.Any
            )
        )

        # We add the Z-value field from the measurement points layer
        self.addParameter(
            QgsProcessingParameterField(
                self.Z_VALUE_FIELD,
                self.tr('Z-value field (numeric)'),
                parentLayerParameterName=self.MEASUREMENT_POINTS,
                type=QgsProcessingParameterField.DataType.Numeric
            )
        )

        # We add a tolerance parameter used to warn about points that are
        # not actually sitting on the line
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOLERANCE,
                self.tr('Tolerance (map units) - warn if a point is farther from the line than this'),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=0.1,
                minValue=0.0
            )
        )

        # We add the interval parameter used to space the generated points
        self.addParameter(
            QgsProcessingParameterNumber(
                self.INTERVAL,
                self.tr('Point interval (map units)'),
                type=QgsProcessingParameterNumber.Type.Double,
                defaultValue=1.0,
                minValue=0.0001
            )
        )

        # We add the output parameter for the line segments.
        segment_output_param = QgsProcessingParameterFeatureSink(
            self.SEGMENT_OUTPUT,
            self.tr('Output line segments'),
            QgsProcessing.SourceType.TypeVectorLine,
            optional=True
        )
        segment_output_param.setCreateByDefault(True)
        self.addParameter(segment_output_param)

        # We add the output parameter for the lines. It's optional and not
        # created by default.
        line_output_param = QgsProcessingParameterFeatureSink(
            self.OUTPUT,
            self.tr('Output simple lines'),
            QgsProcessing.SourceType.TypeVectorLine,
            optional=True
        )
        line_output_param.setCreateByDefault(False)
        self.addParameter(line_output_param)

        # We add the output parameter for the generated points. It's also
        # optional and not created by default.
        point_output_param = QgsProcessingParameterFeatureSink(
            self.POINT_OUTPUT,
            self.tr('Output points'),
            QgsProcessing.SourceType.TypeVectorPoint,
            optional=True
        )
        point_output_param.setCreateByDefault(False)
        self.addParameter(point_output_param)

    def processAlgorithm(self, parameters, context, feedback):
        """
        Here is where the processing itself takes place.
        """

        # Retrieve the input layers as feature sources - this respects the
        # "Selected features only" checkbox if the user ticked it
        line_layer = self.parameterAsSource(
            parameters,
            self.LINE_LAYER,
            context
        )

        points_layer = self.parameterAsSource(
            parameters,
            self.MEASUREMENT_POINTS,
            context
        )

        # Retrieve field names
        line_id_field = self.parameterAsString(
            parameters,
            self.LINE_ID_FIELD,
            context
        )

        point_id_field = self.parameterAsString(
            parameters,
            self.POINT_ID_FIELD,
            context
        )

        z_value_field = self.parameterAsString(
            parameters,
            self.Z_VALUE_FIELD,
            context
        )

        tolerance = self.parameterAsDouble(
            parameters,
            self.TOLERANCE,
            context
        )

        interval = self.parameterAsDouble(
            parameters,
            self.INTERVAL,
            context
        )

        # If layers were not found, throw an exception
        if line_layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.LINE_LAYER)
            )

        if points_layer is None:
            raise QgsProcessingException(
                self.invalidSourceError(parameters, self.MEASUREMENT_POINTS)
            )

        if interval <= 0:
            raise QgsProcessingException(
                self.tr('Point interval must be greater than 0.')
            )

        line_crs = line_layer.sourceCrs()

        # Send some information to the user
        feedback.pushInfo(
            self.tr('Processing CRS: {}').format(line_crs.authid())
        )

        # Stations (M), tolerance and interval are all computed with planar
        # geometry in the line layer's map units. In a geographic CRS those
        # units are degrees, which makes all three meaningless (an interval
        # of 1 would be roughly 75 to 110 km). Refuse instead of silently
        # producing degree-based stations.
        if line_crs.isGeographic():
            raise QgsProcessingException(
                self.tr(
                    'The line layer uses a geographic CRS ({}), so stations, '
                    'tolerance and interval would be measured in degrees. '
                    'Reproject the line layer to a projected CRS in meters '
                    'first (e.g. with "Reproject layer").'
                ).format(line_crs.authid())
            )

        map_units = line_crs.mapUnits()
        feedback.pushInfo(
            self.tr(
                "Stations (M values), tolerance and interval are in the line "
                "layer's map units: {}."
            ).format(QgsUnitTypes.toString(map_units))
        )

        # Planar distances in a projected CRS are not true ground distances:
        # every projection has a scale factor (small within a local
        # Gauss-Krueger or UTM zone, very large in Web Mercator). Each
        # processed line is also measured on the ellipsoid, and the overall
        # deviation is reported at the end. The ellipsoid comes from the
        # project if one is set, otherwise from the line layer's own CRS -
        # same datum, so this only undoes the projection and needs no datum
        # transformation.
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(line_crs, context.transformContext())
        ellipsoid = context.ellipsoid()
        if not ellipsoid or ellipsoid.upper() == 'NONE':
            ellipsoid = line_crs.ellipsoidAcronym()
        if ellipsoid:
            distance_area.setEllipsoid(ellipsoid)
        check_ground_distance = (
            distance_area.willUseEllipsoid()
            and map_units != Qgis.DistanceUnit.Unknown
        )
        to_meters = QgsUnitTypes.fromUnitToUnitFactor(map_units, Qgis.DistanceUnit.Meters)
        planar_length_total = 0.0
        ground_length_total = 0.0

        # If the measurement points layer isn't in the same CRS as the line
        # layer, all the geometry math below (lineLocatePoint, distance,
        # interpolate) would silently operate on mismatched coordinates.
        # Build a transform up front and reproject each point on the fly
        # instead of assuming both inputs already share a CRS.
        point_transform = None
        if points_layer.sourceCrs() != line_crs:
            point_transform = QgsCoordinateTransform(
                points_layer.sourceCrs(),
                line_crs,
                context.transformContext()
            )
            feedback.pushInfo(
                self.tr(
                    'Measurement points layer CRS ({}) differs from the line '
                    'layer CRS ({}); points will be reprojected on the fly.'
                ).format(points_layer.sourceCrs().authid(), line_crs.authid())
            )

        # Create fields object from the input line layer (for the line output)
        fields = line_layer.fields()

        # Create output sink for the lines. This output is optional and
        # skipped by default, so `sink` may legitimately be None if the
        # user hasn't enabled it - that's not an error condition.
        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.Type.LineStringZM,  # Use ZM geometry type
            line_crs
        )

        # Build the fields for the point output: the line ID field (matching
        # its original type), plus regular attribute fields carrying the same
        # M and Z values that are also stored in the point geometry, plus a
        # flag saying whether the point is an original measurement or a
        # generated one.
        line_id_field_type = line_layer.fields().field(
            line_layer.fields().indexOf(line_id_field)
        ).type()

        # The point and segment outputs create their own "station",
        # "measure" and "point_type" fields, and "fid" is the primary key
        # of a GeoPackage output (repeated values there make every repeated
        # insert fail). If the Line ID field has one of those names, it gets
        # a distinct name in these two outputs.
        out_id_field = line_id_field
        if line_id_field.lower() in RESERVED_OUTPUT_FIELDS:
            out_id_field = 'line_' + line_id_field
            feedback.pushInfo(
                self.tr(
                    'The Line ID field "{}" is named "{}" in the point and '
                    'segment outputs, to avoid a clash with a field of the '
                    'same name there.'
                ).format(line_id_field, out_id_field)
            )

        point_fields = QgsFields()
        point_fields.append(QgsField(out_id_field, line_id_field_type))
        point_fields.append(_make_field('station', 'Double', 'Double'))
        point_fields.append(_make_field('measure', 'Double', 'Double'))
        point_fields.append(_make_field('point_type', 'QString', 'String'))

        # Create output sink for the line segments. Uses the same field
        # schema as the points, since each segment carries the attributes
        # of its first point. This output is optional but created by
        # default, so `segment_sink` will normally not be None.
        (segment_sink, segment_dest_id) = self.parameterAsSink(
            parameters,
            self.SEGMENT_OUTPUT,
            context,
            point_fields,
            QgsWkbTypes.Type.LineString,
            line_crs
        )

        # Create output sink for the points. Also optional and skipped by
        # default, so `point_sink` may legitimately be None.
        (point_sink, point_dest_id) = self.parameterAsSink(
            parameters,
            self.POINT_OUTPUT,
            context,
            point_fields,
            QgsWkbTypes.Type.PointZM,
            line_crs
        )

        # Index measurement points by line ID. Points are keyed by a
        # normalized numeric form of the point ID field so that, e.g., a
        # Line ID field stored as Integer and a Point ID field stored as
        # Double still match correctly as long as both refer to the same
        # underlying numeric ID. Only the two attributes that are actually
        # used are fetched.
        point_request = QgsFeatureRequest().setSubsetOfAttributes(
            [point_id_field, z_value_field], points_layer.fields()
        )
        points_by_line = {}
        for point_feature in points_layer.getFeatures(point_request):
            point_fid = point_feature.id()
            point_id = point_feature[point_id_field]

            # A point without an ID can't belong to any line. It must not be
            # used as a lookup key either: a line whose ID is also NULL would
            # then "match" every such point. QgsVariantUtils.isNull() covers
            # both Qt5 (NULL QVariant) and Qt6 (None).
            if QgsVariantUtils.isNull(point_id):
                feedback.pushWarning(
                    self.tr(
                        'Measurement point with feature ID {} has no value in '
                        'the Point ID field; skipping it.'
                    ).format(point_fid)
                )
                continue

            # A NULL Z-value would otherwise reach the linear interpolation
            # arithmetic further down and abort the whole run with an
            # unhandled exception. Skip it here with a clear warning instead.
            z_value = point_feature[z_value_field]
            if QgsVariantUtils.isNull(z_value):
                feedback.pushWarning(
                    self.tr(
                        "Measurement point with feature ID {} ({} = '{}') has "
                        "no value in the Z-value field; skipping it."
                    ).format(point_fid, point_id_field, point_id)
                )
                continue

            # A feature without geometry would make asPoint() raise and
            # abort the whole run.
            point_geom = point_feature.geometry()
            if point_geom.isNull() or point_geom.isEmpty():
                feedback.pushWarning(
                    self.tr(
                        "Measurement point with feature ID {} ({} = '{}') has "
                        "no geometry; skipping it."
                    ).format(point_fid, point_id_field, point_id)
                )
                continue

            # Multipoint layers are accepted as point input. A single-part
            # multipoint is simply its point; with several parts only the
            # first one is used, as for multipart lines.
            if point_geom.isMultipart():
                parts = point_geom.asMultiPoint()
                if len(parts) > 1:
                    feedback.pushWarning(
                        self.tr(
                            "Measurement point with feature ID {} ({} = '{}') "
                            "has {} parts; only the first part is used."
                        ).format(point_fid, point_id_field, point_id, len(parts))
                    )
                point_geom = QgsGeometry.fromPointXY(parts[0])

            if point_transform is not None:
                try:
                    point_geom.transform(point_transform)
                except Exception as exc:
                    feedback.pushWarning(
                        self.tr(
                            "Could not reproject measurement point with "
                            "feature ID {} ({} = '{}') into the line layer's "
                            "CRS; skipping it. ({})"
                        ).format(point_fid, point_id_field, point_id, exc)
                    )
                    continue

            point_xy = point_geom.asPoint()
            points_by_line.setdefault(_numeric_key(point_id), []).append(
                {
                    "x": point_xy.x(),
                    "y": point_xy.y(),
                    "z_value": z_value,
                    "fid": point_fid,
                }
            )

        # Compute the number of steps to display within the progress bar
        total = line_layer.featureCount()
        step_size = 100.0 / total if total > 0 else 0

        # Track how many lines actually found a matching set of measurement
        # points, so we can warn about a systematic ID mismatch afterwards.
        lines_matched = 0
        lines_seen = 0
        lines_processed = 0

        # Lines skipped for routine reasons are collected and reported once
        # at the end instead of one warning per line, which would otherwise
        # bury the warnings that need attention (e.g. points far off their
        # line) in a long log.
        lines_without_points = []
        lines_without_id = 0
        curved_lines = 0

        # addFeature() can return False on a failed write (e.g. a
        # provider-level error) without raising an exception - it fails
        # silently unless the return value is checked. Count failures here
        # instead of warning per-feature, to avoid flooding the log, and
        # report a single summary at the end.
        line_write_failures = 0
        point_write_failures = 0
        segment_write_failures = 0

        # Only fetch the line ID, unless the simple-lines output (which
        # keeps every attribute of the input lines) is requested.
        line_request = QgsFeatureRequest()
        if sink is None:
            line_request.setSubsetOfAttributes([line_id_field], line_layer.fields())

        # Process each line
        for current, line_feature in enumerate(line_layer.getFeatures(line_request)):
            # Stop the algorithm if cancel button has been clicked
            if feedback.isCanceled():
                break

            lines_seen += 1
            line_id = line_feature[line_id_field]

            # A line without an ID can't be matched to any points.
            if QgsVariantUtils.isNull(line_id):
                lines_without_id += 1
                feedback.setProgress(int((current + 1) * step_size))
                continue

            line_id_key = _numeric_key(line_id)

            # Get measurement points for this line
            if line_id_key not in points_by_line:
                lines_without_points.append(line_id)
                feedback.setProgress(int((current + 1) * step_size))
                continue

            lines_matched += 1

            # A single measurement point is accepted: its value is
            # applied uniformly along the whole line (see interpolated_z()
            # below, which naturally degenerates to a flat value when there
            # is only one measurement station to bisect against).
            if len(points_by_line[line_id_key]) == 1:
                feedback.pushInfo(
                    self.tr(
                        "Line '{}': Only one measurement point found; its "
                        "value will be applied uniformly along the whole line."
                    ).format(line_id)
                )

            line_geom = line_feature.geometry()

            if line_geom is None or line_geom.isNull() or line_geom.isEmpty():
                feedback.pushWarning(
                    self.tr("Line '{}': Empty or missing geometry. Skipping line.").format(line_id)
                )
                feedback.setProgress(int((current + 1) * step_size))
                continue

            # Curved geometries (CircularString / CompoundCurve, e.g. from
            # the curve digitizing tools) store arc control points as their
            # vertices. Summing straight distances between those gives chord
            # lengths, while lineLocatePoint() measures along the arc, so the
            # two station systems would disagree. Convert to straight
            # segments first so all stations refer to the same geometry.
            if QgsWkbTypes.isCurvedType(line_geom.wkbType()):
                line_geom.convertToStraightSegment()
                curved_lines += 1

            # Convert multipart to single part if needed
            if line_geom.isMultipart():
                parts = line_geom.asMultiPolyline()
                if len(parts) > 0:
                    if len(parts) > 1:
                        feedback.pushWarning(
                            self.tr(
                                "Line '{}': Multipart line has {} parts; only "
                                "the first part is used, the rest are ignored."
                            ).format(line_id, len(parts))
                        )
                    # Reconstruct as a single-part geometry
                    line_geom = QgsGeometry(QgsLineString(parts[0]))
                else:
                    feedback.pushWarning(
                        self.tr("Line '{}': Empty multipart geometry.").format(line_id)
                    )
                    feedback.setProgress(int((current + 1) * step_size))
                    continue

            # Keep the line's own shape: record every original vertex
            # together with its station (distance along the line). This is
            # computed by summing the distance between consecutive vertices
            # in their actual order, rather than via lineLocatePoint on each
            # vertex.
            shape_vertices = []
            cumulative_distance = 0.0
            previous_xy = None
            for vertex in line_geom.vertices():
                vx, vy = vertex.x(), vertex.y()
                if previous_xy is not None:
                    cumulative_distance += math.hypot(vx - previous_xy[0], vy - previous_xy[1])
                shape_vertices.append(
                    {"station": cumulative_distance, "x": vx, "y": vy, "is_measurement": False}
                )
                previous_xy = (vx, vy)

            if cumulative_distance <= STATION_EPSILON:
                feedback.pushWarning(
                    self.tr("Line '{}': Line has zero length. Skipping line.").format(line_id)
                )
                feedback.setProgress(int((current + 1) * step_size))
                continue

            # Snap each measurement point onto the line, and record its
            # station. Points should already sit on the line, so we also
            # check the actual distance against the tolerance and warn if
            # it's exceeded (the point is still used, snapped to the line).
            measurement_vertices = []
            for point in points_by_line[line_id_key]:
                point_geom = QgsGeometry(QgsPoint(point["x"], point["y"]))
                station = line_geom.lineLocatePoint(point_geom)
                snapped_geom = line_geom.interpolate(station)

                if snapped_geom.isEmpty():
                    feedback.pushWarning(
                        self.tr(
                            "Line '{}': Could not project measurement point "
                            "with feature ID {} onto the line. Skipping point."
                        ).format(line_id, point["fid"])
                    )
                    continue

                snapped_point = snapped_geom.asPoint()

                distance_off_line = point_geom.distance(line_geom)
                if distance_off_line > tolerance:
                    feedback.pushWarning(
                        self.tr(
                            "Line '{}': measurement point with feature ID {} "
                            "(value {}) is {:.4f} map units away from the "
                            "line (tolerance is {})."
                        ).format(line_id, point["fid"], _fmt(point["z_value"]), distance_off_line, tolerance)
                    )

                measurement_vertices.append(
                    {
                        "station": station,
                        "x": snapped_point.x(),
                        "y": snapped_point.y(),
                        "is_measurement": True,
                        "z_value": point["z_value"],
                        "fid": point["fid"],
                        "distance": distance_off_line,
                    }
                )

            if len(measurement_vertices) < 1:
                feedback.pushWarning(
                    self.tr(
                        "Line '{}': No valid measurement points remained "
                        "after projecting onto the line. Skipping line."
                    ).format(line_id)
                )
                feedback.setProgress(int((current + 1) * step_size))
                continue

            # Merge the line's own vertices with the measurement points,
            # sorted by station.
            all_vertices = shape_vertices + measurement_vertices
            all_vertices.sort(key=lambda v: v["station"])

            # Drop near-duplicate stations (e.g. a measurement point that
            # falls exactly on an existing line vertex), preferring the
            # measurement point's data so its Z-value is kept exactly. Two
            # measurement points at the same position can't both be used.
            # The one closer to the line wins: points beyond a line end are
            # all snapped onto that end, and a point far off the line (often
            # assigned to the wrong line) must not win just because it comes
            # first in the attribute table. If both have the same value, the
            # choice doesn't affect the result and is only reported as
            # information.
            merged_vertices = []
            for v in all_vertices:
                if (merged_vertices
                        and abs(v["station"] - merged_vertices[-1]["station"]) <= STATION_EPSILON):
                    previous = merged_vertices[-1]
                    if v["is_measurement"] and not previous["is_measurement"]:
                        merged_vertices[-1] = v
                    elif v["is_measurement"] and previous["is_measurement"]:
                        if v["distance"] < previous["distance"]:
                            kept, ignored = v, previous
                        else:
                            kept, ignored = previous, v
                        merged_vertices[-1] = kept
                        if kept["z_value"] == ignored["z_value"]:
                            feedback.pushInfo(
                                self.tr(
                                    "Line '{}': measurement points with feature "
                                    "IDs {} and {} snap to the same position on "
                                    "the line and have the same value; feature "
                                    "ID {} is ignored."
                                ).format(line_id, kept["fid"], ignored["fid"], ignored["fid"])
                            )
                        else:
                            feedback.pushWarning(
                                self.tr(
                                    "Line '{}': measurement points with feature "
                                    "IDs {} and {} are at the same position "
                                    "(station {:.3f}). The one closer to the line "
                                    "is used: feature ID {} (value {}, {:.4f} map "
                                    "units away); feature ID {} (value {}, {:.4f} "
                                    "map units away) is ignored."
                                ).format(line_id, kept["fid"], ignored["fid"], kept["station"],
                                         kept["fid"], _fmt(kept["z_value"]), kept["distance"],
                                         ignored["fid"], _fmt(ignored["z_value"]), ignored["distance"])
                            )
                    continue
                merged_vertices.append(v)

            # Interpolate Z linearly between measurement points for the
            # vertices that came from the line's own geometry. Vertices
            # before the first / after the last measurement point take that
            # point's value (flat extrapolation).
            #
            # Note on edge cases:
            # - With only one measurement station, bisect_left against a
            #   single-element list always resolves to either idx<=0 or
            #   idx>=len(...), so every vertex falls into the flat
            #   extrapolation branches and returns that one value - i.e.
            #   the whole line is treated as "interpolated" from a single
            #   point, which is exactly a constant.
            # - When two neighboring measurement values are equal
            #   (v0 == v1), t * (v1 - v0) is t * 0 regardless of t, so the
            #   result simplifies to v0 for every station in between. No
            #   division-by-zero risk is introduced by equal *values* -
            #   the only division guard needed is for equal *stations*
            #   (s1 == s0), which is already handled below.
            measurement_stations = [v["station"] for v in merged_vertices if v["is_measurement"]]
            measurement_values = [v["z_value"] for v in merged_vertices if v["is_measurement"]]

            def interpolated_z(station):
                idx = bisect.bisect_left(measurement_stations, station)
                if idx <= 0:
                    return measurement_values[0]
                if idx >= len(measurement_stations):
                    return measurement_values[-1]
                s0, s1 = measurement_stations[idx - 1], measurement_stations[idx]
                v0, v1 = measurement_values[idx - 1], measurement_values[idx]
                if s1 == s0:
                    return v0
                t = (station - s0) / (s1 - s0)
                return v0 + t * (v1 - v0)

            # Create LineStringZM with Z-values from the selected field
            # (interpolated at the line's own vertices) and M-values as station
            points_zm = [
                QgsPoint(
                    v["x"],
                    v["y"],
                    z=v["z_value"] if v["is_measurement"] else interpolated_z(v["station"]),
                    m=v["station"],
                )
                for v in merged_vertices
            ]

            line_zm = QgsLineString(points_zm)
            total_length = merged_vertices[-1]["station"]

            # Compare the planar line length with the ground distance on the
            # ellipsoid (reported once at the end).
            if check_ground_distance:
                ground_length = distance_area.measureLength(line_geom)
                if ground_length > 0 and math.isfinite(ground_length):
                    planar_length_total += total_length * to_meters
                    ground_length_total += ground_length

            # Write feature to the line output sink (if that output wasn't
            # skipped), keeping every attribute of the input line.
            if sink is not None:
                feature = QgsFeature(fields)
                feature.setGeometry(line_zm)
                feature.setAttributes(line_feature.attributes())
                if not sink.addFeature(feature, FAST_INSERT):
                    line_write_failures += 1

            # Generate points at a regular interval along the line, used
            # for both the point output and the line segment output (if
            # either of them wasn't skipped). Where an input measurement
            # point already falls within half an interval of a regularly
            # spaced position, reuse that measurement point instead of
            # generating a new one there.
            if point_sink is not None or segment_sink is not None:
                # Regular stations computed as i * interval rather than by
                # repeatedly adding the interval: repeated addition
                # accumulates floating point error and could add a second,
                # near-identical point just before the line's end (and with
                # it a zero-length segment). Rounding to 9 decimals keeps
                # values like 0.6000000000000001 out of the attribute table
                # (the interval is at least 0.0001, so nothing is lost). The
                # line's end is always included.
                step_count = int(math.floor(total_length / interval + 1e-9))
                grid_stations = [round(i * interval, 9) for i in range(step_count + 1)]
                if total_length - grid_stations[-1] > STATION_EPSILON:
                    grid_stations.append(total_length)
                else:
                    grid_stations[-1] = total_length

                def nearest_measurement_distance(s):
                    idx = bisect.bisect_left(measurement_stations, s)
                    candidates = []
                    if idx < len(measurement_stations):
                        candidates.append(abs(measurement_stations[idx] - s))
                    if idx > 0:
                        candidates.append(abs(measurement_stations[idx - 1] - s))
                    return min(candidates) if candidates else float('inf')

                output_points = []

                # Always output the real measurement points
                for v in merged_vertices:
                    if v["is_measurement"]:
                        output_points.append(
                            {
                                "station": v["station"],
                                "x": v["x"],
                                "y": v["y"],
                                "z_value": v["z_value"],
                                "point_type": "measurement",
                            }
                        )

                # Add generated grid points, skipping ones too close to a
                # measurement point (the measurement point is used instead).
                # "Too close" means within half an interval, so a generated
                # point a full interval away from a measurement point is kept
                # rather than leaving a gap of two intervals. The line's start
                # and end are only skipped if a measurement point sits exactly
                # there - otherwise the segments would not cover the whole
                # line.
                line_start_station, line_end_station = grid_stations[0], grid_stations[-1]
                for s in grid_stations:
                    if s in (line_start_station, line_end_station):
                        skip_distance = STATION_EPSILON
                    else:
                        skip_distance = interval / 2
                    if nearest_measurement_distance(s) <= skip_distance:
                        continue

                    grid_point_geom = line_geom.interpolate(s)
                    if grid_point_geom.isEmpty():
                        continue
                    grid_point = grid_point_geom.asPoint()

                    output_points.append(
                        {
                            "station": s,
                            "x": grid_point.x(),
                            "y": grid_point.y(),
                            "z_value": interpolated_z(s),
                            "point_type": "generated",
                        }
                    )

                output_points.sort(key=lambda p: p["station"])

                if point_sink is not None:
                    for p in output_points:
                        point_feature = QgsFeature(point_fields)
                        point_feature.setGeometry(
                            QgsGeometry(QgsPoint(p["x"], p["y"], z=p["z_value"], m=p["station"]))
                        )
                        point_feature[out_id_field] = line_id
                        point_feature['station'] = p["station"]
                        point_feature['measure'] = p["z_value"]
                        point_feature['point_type'] = p["point_type"]
                        if not point_sink.addFeature(point_feature, FAST_INSERT):
                            point_write_failures += 1

                # Build one individual line feature per segment between
                # two consecutive output points, carrying the attributes of
                # the segment's first (start) point.
                if segment_sink is not None:
                    for p_start, p_end in zip(output_points[:-1], output_points[1:]):
                        segment_geom = QgsGeometry(
                            QgsLineString([
                                QgsPoint(p_start["x"], p_start["y"]),
                                QgsPoint(p_end["x"], p_end["y"]),
                            ])
                        )

                        segment_feature = QgsFeature(point_fields)
                        segment_feature.setGeometry(segment_geom)
                        segment_feature[out_id_field] = line_id
                        segment_feature['station'] = p_start["station"]
                        segment_feature['measure'] = p_start["z_value"]
                        segment_feature['point_type'] = p_start["point_type"]
                        if not segment_sink.addFeature(segment_feature, FAST_INSERT):
                            segment_write_failures += 1

            lines_processed += 1

            # Update the progress bar
            feedback.setProgress(int((current + 1) * step_size))

        if lines_without_id:
            feedback.pushWarning(
                self.tr(
                    '{} line(s) have no value in the Line ID field and were '
                    'skipped.'
                ).format(lines_without_id)
            )

        # One summary for all lines without measurement points, including an
        # expression that selects exactly those lines in the line layer.
        if lines_without_points:
            id_list = ', '.join(QgsExpression.quotedValue(v) for v in lines_without_points)
            feedback.pushWarning(
                self.tr(
                    '{} of {} line(s) have no matching measurement points and '
                    'were skipped. To select them in the line layer, use this '
                    'expression: {} IN ({})'
                ).format(len(lines_without_points), lines_seen,
                         QgsExpression.quotedColumnRef(line_id_field), id_list)
            )

        if curved_lines:
            feedback.pushInfo(
                self.tr(
                    '{} curved line geometries were converted to straight '
                    'segments before processing.'
                ).format(curved_lines)
            )

        # If not a single line matched any measurement points, but both
        # layers actually have features, this is almost always an ID
        # field mismatch (e.g. text vs. numeric formatting) rather than a
        # genuine data gap - flag it explicitly so it's not mistaken for
        # a silent no-op.
        if total > 0 and points_layer.featureCount() > 0 and lines_matched == 0:
            feedback.pushWarning(
                self.tr(
                    'No line matched any measurement points at all. This '
                    'usually means the Line ID field and Point ID field do '
                    'not actually refer to the same IDs - double-check both '
                    'fields and their values.'
                )
            )

        # Report how far the planar stations are from the true ground
        # distance. A large deviation (e.g. Web Mercator) means the station
        # values and the interval are not real meters on the ground.
        if ground_length_total > 0:
            deviation = (planar_length_total / ground_length_total - 1.0) * 100.0
            message = self.tr(
                'Station values are planar lengths in the line layer\'s CRS; '
                'compared with the ground distance on the ellipsoid ({}) they '
                'are off by {:+.3f} %.'
            ).format(distance_area.ellipsoid(), deviation)
            if abs(deviation) > GROUND_DEVIATION_WARN_PERCENT:
                feedback.pushWarning(
                    message + ' ' + self.tr(
                        'Consider a projected CRS with less scale distortion '
                        'for this area (e.g. the local Gauss-Krueger or UTM '
                        'zone).'
                    )
                )
            else:
                feedback.pushInfo(message)

        # Report any silently-failed writes (addFeature() returns False on
        # failure instead of raising) as a single summary rather than
        # per-feature, so a systematic provider issue is surfaced clearly
        # without flooding the log.
        if line_write_failures or point_write_failures or segment_write_failures:
            feedback.reportError(
                self.tr(
                    'Some features could not be written to the output '
                    'sink(s): {} line(s), {} point(s), {} segment(s) failed.'
                ).format(line_write_failures, point_write_failures, segment_write_failures),
                fatalError=False
            )

        # Send completion message
        if not feedback.isCanceled():
            feedback.pushInfo(
                self.tr('Finished: {} line(s) processed.').format(lines_processed)
            )

        # Return the results of the algorithm
        return {
            self.OUTPUT: dest_id,
            self.POINT_OUTPUT: point_dest_id,
            self.SEGMENT_OUTPUT: segment_dest_id,
        }
