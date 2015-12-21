﻿  # -*- coding: utf-8 -*-
'''
Created on 4 Mar 2014

@author: Kimon Tsitsikas

Copyright © 2014 Kimon Tsitsikas, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms 
of the GNU General Public License version 2 as published by the Free Software 
Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; 
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR 
PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with 
Odemis. If not, see http://www.gnu.org/licenses/.
'''
from __future__ import division
from __future__ import absolute_import

import logging
import math
import numpy
from odemis import model, util
from odemis.model import isasync, CancellableThreadPoolExecutor
import threading
import time
import weakref
from odemis.util import TimeoutError
from tescan import sem, CancelledError
import re
from odemis.model import HwError
import Queue
from odemis.model import roattribute, oneway
import gc
import socket


ACQ_CMD_UPD = 1
ACQ_CMD_TERM = 2

class SEM(model.HwComponent):
    '''
    This is an extension of the model.HwComponent class. It instantiates the scanner 
    and se-detector children components and provides an update function for its 
    metadata. 
    '''

    def __init__(self, name, role, children, host, daemon=None, **kwargs):
        '''
        children (dict string->kwargs): parameters setting for the children.
            Known children are "scanner", "detector", "stage", "focus", "camera"
            and "pressure". They will be provided back in the .children VA
        host (string): ip address of the SEM server 
        Raise an exception if the device cannot be opened
        '''
        # we will fill the set of children with Components later in ._children
        model.HwComponent.__init__(self, name, role, daemon=daemon, **kwargs)

        self._host = host
        self._device = sem.Sem()
        logging.info("Going to connect to host")
        result = self._device.Connect(host, 8300)
        if result < 0:
            raise HwError("Failed to connect to TESCAN server '%s'. "
                          "Check that the ip address is correct and TESCAN server "
                          "connected to the network." % (host,))
        logging.info("Connected")
        self._device.connection.socket_c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._device.connection.socket_d.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Lock in order to synchronize all the child component functions
        # that acquire data from the SEM while we continuously acquire images
        self._acq_progress_lock = threading.Lock()
        self._acquisition_mng_lock = threading.Lock()
        self._acquisition_init_lock = threading.Lock()
        self._acq_cmd_q = Queue.Queue()
        self._acquisition_must_stop = threading.Event()
        self._acquisitions = set()  # detectors currently active

        # important: stop the scanning before we start scanning or before automatic
        # procedures, even before we configure the detectors
        self._device.ScStopScan()
        # Blanker is automatically enabled when no scanning takes place
        self._device.ScSetBlanker(1, 2)

        self._hwName = "TescanSEM (s/n: %s)" % (self._device.TcpGetDevice())
        self._metadata[model.MD_HW_NAME] = self._hwName
        self._swVersion = "SEM sw %s, protocol %s" % (self._device.TcpGetSWVersion(),
                                                      self._device.TcpGetVersion())
        self._metadata[model.MD_SW_VERSION] = self._swVersion

        # create the scanner child
        try:
            kwargs = children["scanner"]
        except (KeyError, TypeError):
            raise KeyError("TescanSEM was not given a 'scanner' child")

        self._scanner = Scanner(parent=self, daemon=daemon, **kwargs)
        self.children.value.add(self._scanner)

        # create the detector children
        self._detectors = {}
        for name, ckwargs in children.items():
            if name.startswith("detector"):
                self._detectors[name] = Detector(parent=self, daemon=daemon, **ckwargs)
                self.children.value.add(self._detectors[name])
        if not self._detectors:
            logging.info("TescanSEM was not given a 'detector' child")

        # create the stage child
        try:
            kwargs = children["stage"]
        except (KeyError, TypeError):
            raise KeyError("TescanSEM was not given a 'stage' child")

        self._stage = Stage(parent=self, daemon=daemon, **kwargs)
        self.children.value.add(self._stage)

        # create the focus child
        try:
            kwargs = children["focus"]
        except (KeyError, TypeError):
            raise KeyError("TescanSEM was not given a 'focus' child")
        self._focus = EbeamFocus(parent=self, daemon=daemon, **kwargs)
        self.children.value.add(self._focus)

        # create the camera child
        try:
            kwargs = children["camera"]
        except (KeyError, TypeError):
            logging.info("Not initialising the chamber camera")
        else:
            self._camera = ChamberView(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._camera)

        # create the pressure child
        try:
            kwargs = children["pressure"]
        except (KeyError, TypeError):
            logging.info("TescanSEM was not given a 'pressure' child")
        else:
            self._pressure = ChamberPressure(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._pressure)

        # create the light child
        try:
            kwargs = children["light"]
        except (KeyError, TypeError):
            logging.info("TescanSEM was not given a 'light' child")
        else:
            self._light = Light(parent=self, daemon=daemon, **kwargs)
            self.children.value.add(self._light)

        self._acquisition_thread = threading.Thread(target=self._acquisition_run,
                                                    name="SEM acquisition thread")
        self._acquisition_thread.start()

    def _reset_device(self):
        logging.info("Resetting device %s", self._hwName)
        self._device = sem.Sem()
        logging.info("Going to connect to host")
        result = self._device.Connect(self._host, 8300)
        if result < 0:
            raise HwError("Failed to connect to TESCAN server '%s'. "
                          "Check that the ip address is correct and TESCAN server "
                          "connected to the network." % (self._host,))
        self._device.ScStopScan()
        logging.info("Connected")

    def start_acquire(self, detector):
        """
        Start acquiring images on the given detector (i.e., input channel).
        detector (Detector): detector from which to acquire an image
        Note: The acquisition parameters are defined by the scanner. Acquisition
        might already be going on for another detector, in which case the detector
        will be added on the next acquisition.
        raises KeyError if the detector is already being acquired.
        """
        with self._acq_progress_lock:
            self._device.ScStopScan()
        # to be thread-safe (simultaneous calls to start/stop_acquire())
        with self._acquisition_mng_lock:
            if detector in self._acquisitions:
                raise KeyError("Channel %d already set up for acquisition." % detector.channel)
            # make sure socket settings are reset
            self._device.connection.socket_c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._device.connection.socket_d.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._acquisitions.add(detector)
            self._acq_cmd_q.put(ACQ_CMD_UPD)

            # If something went wrong with the thread, report also here
            if self._acquisition_thread is None:
                raise IOError("Acquisition thread is gone, cannot acquire")

    def stop_acquire(self, detector):
        """
        Stop acquiring images on the given channel.
        detector (Detector): detector from which to acquire an image
        Note: acquisition might still go on on other channels
        """
        with self._acquisition_mng_lock:
            self._acquisitions.discard(detector)
            self._acq_cmd_q.put(ACQ_CMD_UPD)
            if not self._acquisitions:
                self._req_stop_acquisition()

    def _check_cmd_q(self, block=True):
        """
        block (bool): if True, will wait for a (just one) command to come,
          otherwise, will wait for no more command to be queued
        raise CancelledError: if the TERM command was received.
        """
        # Read until there are no more commands
        while True:
            try:
                cmd = self._acq_cmd_q.get(block=block)
            except Queue.Empty:
                break

            # Decode command
            if cmd == ACQ_CMD_UPD:
                pass
            elif cmd == ACQ_CMD_TERM:
                logging.debug("Acquisition thread received terminate command")
                raise CancelledError("Terminate command received")
            else:
                logging.error("Unexpected command %s", cmd)

            if block:
                return

    def _acquisition_run(self):
        """
        Acquire images until asked to stop. Sends the raw acquired data to the
          callbacks.
        Note: to be run in a separate thread
        """
        last_gc = 0
        nfailures = 0
        try:
            while True:
                with self._acquisition_init_lock:
                    # Ensure that if a rest/term is needed, and we don't catch
                    # it in the queue (yet), it will be detected before starting
                    # the read/write commands.
                    self._acquisition_must_stop.clear()

                self._check_cmd_q(block=False)

                detectors = tuple(self._acq_wait_detectors_ready())  # ordered
                if detectors:
                    with self._acq_progress_lock:
                        # Beam ON
                        self._device.ScSetBlanker(1, 0)
                    # write and read the raw data
                    try:
                        rdas = self._acquire_detectors(detectors)
                    except CancelledError:
                        # either because must terminate or just need to rest
                        logging.debug("Acquisition was cancelled")
                        continue
                    except Exception as e:
                        logging.exception(e)
                        # could be genuine or just due to cancellation
                        self._check_cmd_q(block=False)

                        nfailures += 1
                        if nfailures == 5:
                            logging.exception("Acquisition failed %d times in a row, giving up", nfailures)
                            return
                        else:
                            logging.exception("Acquisition failed, will retry")
                            time.sleep(1)
                            self._reset_device()
                            continue

                    nfailures = 0

                    for d, da in zip(detectors, rdas):
                        d.data.notify(da)

                    # force the GC to non-used buffers, for some reason, without this
                    # the GC runs only after we've managed to fill up the memory
                    if time.time() - last_gc > 2:  # Costly, so not too often
                        gc.collect()  # TODO: if scan is long enough, during scan
                        last_gc = time.time()
                else:  # nothing to acquire => rest
                    with self._acq_progress_lock:
                        # Beam blanker back to automatic
                        self._device.ScStopScan()
                        # Beam blanker back to automatic
                        self._device.ScSetBlanker(1, 2)
                    gc.collect()
                    # wait until something new comes in
                    self._check_cmd_q(block=True)
                    last_gc = time.time()
        except CancelledError:
            logging.info("Acquisition threading terminated on request")
        except Exception:
            logging.exception("Unexpected failure during image acquisition")
        finally:
            try:
                with self._acq_progress_lock:
                    self._device.ScStopScan()
                    # Beam blanker back to automatic
                    self._device.ScSetBlanker(1, 2)
            except Exception:
                # can happen if the driver already terminated
                pass
            logging.info("Acquisition thread closed")
            self._acquisition_thread = None

    def _req_stop_acquisition(self):
        """
        Request the acquisition thread to stop
        """
        with self._acquisition_init_lock:
            self._acquisition_must_stop.set()
            self._device.ScStopScan()
            self._device.CancelRecv()

    def _acq_wait_detectors_ready(self):
        """
        Block until all the detectors to acquire are ready (ie, received
          synchronisation event, or not synchronized)
        returns (set of Detectors): the detectors to acquire from
        """
        detectors = self._acquisitions.copy()
        det_not_ready = detectors.copy()
        det_ready = set()
        while det_not_ready:
            # Wait for all the DataFlows to have received a sync event
            for d in det_not_ready:
                d.data._waitSync()
                det_ready.add(d)

            # Check if new detectors were added (or removed) in the mean time
            with self._acquisition_mng_lock:
                detectors = self._acquisitions.copy()
            det_not_ready = detectors - det_ready

        return detectors

    def _acquire_detectors(self, detectors):
        """
        Run the acquisition for multiple detectors
        return (list of DataArrays): acquisition for each detector in order
        """
        rdas = []
        for d in detectors:
            rbuf = self._single_acquisition(d.channel)
            rdas.append(rbuf)

        return rdas

    def _single_acquisition(self, channel):
        with self._acq_progress_lock:
            with self._acquisition_init_lock:
                if self._acquisition_must_stop.is_set():
                    raise CancelledError("Acquisition cancelled during preparation")
            pxs = self._scanner.pixelSize.value  # m/px

            pxs_pos = self._scanner.translation.value
            scale = self._scanner.scale.value
            res = (self._scanner.resolution.value[0],
                   self._scanner.resolution.value[1])

            metadata = dict(self._metadata)
            phy_pos = metadata.get(model.MD_POS, (0, 0))
            trans = self._scanner.pixelToPhy(pxs_pos)
            updated_phy_pos = (phy_pos[0] + trans[0], phy_pos[1] + trans[1])

            # update changed metadata
            metadata[model.MD_POS] = updated_phy_pos
            metadata[model.MD_PIXEL_SIZE] = (pxs[0] * scale[0], pxs[1] * scale[1])
            metadata[model.MD_ACQ_DATE] = time.time()
            metadata[model.MD_ROTATION] = self._scanner.rotation.value
            metadata[model.MD_DWELL_TIME] = self._scanner.dwellTime.value

            scaled_shape = (self._scanner._shape[0] / scale[0], self._scanner._shape[1] / scale[1])
            scaled_trans = (pxs_pos[0] / scale[0], pxs_pos[1] / scale[1])
            center = (scaled_shape[0] / 2, scaled_shape[1] / 2)
            l = int(center[0] + scaled_trans[0] - (res[0] / 2))
            t = int(center[1] + scaled_trans[1] - (res[1] / 2))
            r = l + res[0] - 1
            b = t + res[1] - 1

            dt = self._scanner.dwellTime.value * 1e9
            logging.debug("Acquiring SEM image of %s with dwell time %f ns", res, dt)
            try:
                # Check if spot mode is required
                if res == (1, 1):
                    # FIXME: Overlook the dwell time, just set it to 1e03 ns and
                    # scan until you leave the spot mode. This is to avoid the
                    # delay in fetching images from Tescan when we are in spot
                    # mode with a dwell time > 1ms.
                    self._device.ScScanLine(0, scaled_shape[0], scaled_shape[1],
                                         l + 1, t + 1, r + 1, b + 1, dt, 1, 1)
                else:
                    self._device.ScScanXY(0, scaled_shape[0], scaled_shape[1],
                                         l, t, r, b, 1, dt)
                # we must stop the scanning even after single scan
                # self._device.ScStopScan()
                # return model.DataArray(numpy.array([[0]], dtype=numpy.uint16), metadata)
                # fetch the image (blocking operation), ndarray is returned
                sem_img = self._device.FetchArray(channel, res[0] * res[1])
            except Exception:
                raise CancelledError("Acquisition cancelled during scanning")
            sem_img.shape = res[::-1]
            # Change endianess
            sem_img.byteswap(True)

            # we must stop the scanning even after single scan
            self._device.ScStopScan()

            return model.DataArray(sem_img, metadata)

    def terminate(self):
        """
        Must be called at the end of the usage. Can be called multiple times,
        but the component shouldn't be used afterwards.
        """
        # Terminate components
        self._stage.terminate()
        self._focus.terminate()
        if hasattr(self, "_camera"):
            self._camera.terminate()
        self._pressure.terminate()
        # finish
        if self._device:
            # stop the acquisition thread
            with self._acquisition_mng_lock:
                self._acquisitions.clear()
                self._acq_cmd_q.put(ACQ_CMD_TERM)
                self._req_stop_acquisition()
            self._acquisition_thread.join(10)
            self._device.Disconnect()
            self._device = None


class Scanner(model.Emitter):
    """
    This is an extension of the model.Emitter class. It contains Vigilant 
    Attributes and setters for magnification, pixel size, translation, resolution,
    scale, rotation and dwell time. Whenever one of these attributes is changed, 
    its setter also updates another value if needed e.g. when scale is changed, 
    resolution is updated, when resolution is changed, the translation is recentered 
    etc. Similarly it subscribes to the VAs of scale and magnification in order 
    to update the pixel size.
    """
    def __init__(self, name, role, parent, fov_range, **kwargs):
        # It will set up ._shape and .parent
        model.Emitter.__init__(self, name, role, parent=parent, **kwargs)

        self._shape = (2048, 2048)

        # This is the field of view when in Tescan Software magnification = 100
        # and working distance = 0,27 m (maximum WD of Mira TC). When working
        # distance is changed (for example when we focus) magnification mention
        # in odemis and Tescan software are expected to be different.
        self._hfw_nomag = 0.195565  # m

        # Get current field of view and compute magnification
        fov = self.parent._device.GetViewField() * 1e-03
        mag = self._hfw_nomag / fov

        # Field of view in Tescan is set in mm
        self.parent._device.SetViewField(self._hfw_nomag * 1e03 / mag)
        self.magnification = model.VigilantAttribute(mag, unit="", readonly=True)

        self.horizontalFoV = model.FloatContinuous(fov, range=fov_range, unit="m",
                                                   setter=self._setHorizontalFOV)
        self.horizontalFoV.subscribe(self._onHorizontalFOV)  # to update metadata

        # pixelSize is the same as MD_PIXEL_SIZE, with scale == 1
        # == smallest size/ between two different ebeam positions
        pxs = (self._hfw_nomag / (self._shape[0] * mag),
               self._hfw_nomag / (self._shape[1] * mag))
        self.pixelSize = model.VigilantAttribute(pxs, unit="m", readonly=True)

        # (.resolution), .translation, .rotation, and .scaling are used to
        # define the conversion from coordinates to a region of interest.

        # (float, float) in px => moves center of acquisition by this amount
        # independent of scale and rotation.
        tran_rng = [(-self._shape[0] / 2, -self._shape[1] / 2),
                    (self._shape[0] / 2, self._shape[1] / 2)]
        self.translation = model.TupleContinuous((0, 0), tran_rng,
                                              cls=(int, long, float), unit="",
                                              setter=self._setTranslation)

        # .resolution is the number of pixels actually scanned. If it's less than
        # the whole possible area, it's centered.
        resolution = (self._shape[0] // 8, self._shape[1] // 8)
        self.resolution = model.ResolutionVA(resolution, [(1, 1), self._shape],
                                             setter=self._setResolution)
        self._resolution = resolution

        # (float, float) as a ratio => how big is a pixel, compared to pixelSize
        # it basically works the same as binning, but can be float
        # (Default to scan the whole area)
        self._scale = (self._shape[0] / resolution[0], self._shape[1] / resolution[1])
        self.scale = model.TupleContinuous(self._scale, [(1, 1), self._shape],
                                           cls=(int, long, float),
                                           unit="", setter=self._setScale)
        self.scale.subscribe(self._onScale, init=True)  # to update metadata

        # (float) in rad => rotation of the image compared to the original axes
        # TODO: for now it's readonly because no rotation is supported
        self.rotation = model.FloatContinuous(0, [0, 2 * math.pi], unit="rad",
                                              readonly=True)

        self.dwellTime = model.FloatContinuous(1e-06, (1e-06, 1000), unit="s")
        self.dwellTime.subscribe(self._onDwellTime)

        # Range is according to min and max voltages accepted by Tescan API
        volt_range = self.GetVoltagesRange()
        volt = self.parent._device.HVGetVoltage()
        self.accelVoltage = model.FloatContinuous(volt, volt_range, unit="V")
        self.accelVoltage.subscribe(self._onVoltage)

        # 0 turns off the e-beam, 1 turns it on
        power_choices = set([0, 1])
        self._power = self.parent._device.HVGetBeam()  # Don't change state
        self.power = model.IntEnumerated(self._power, power_choices, unit="",
                                  setter=self._setPower)

        # Enumerated float with respect to the PC indexes of Tescan API
        self._list_currents = self.GetProbeCurrents()
        pc_choices = set(self._list_currents)
        # We use the current PC
        self._probeCurrent = self._list_currents[self.parent._device.GetPCIndex() - 1]
        self.probeCurrent = model.FloatEnumerated(self._probeCurrent, pc_choices, unit="A",
                                  setter=self._setPC)

        # None implies that there is a blanker but it is set automatically.
        # Mostly used in order to know if the module supports beam blanking
        # when accessing it from outside.
        self.blanker = model.VAEnumerated(None, choices=set([None]))

    # we share metadata with our parent
    def updateMetadata(self, md):
        self.parent.updateMetadata(md)

    def getMetadata(self):
        return self.parent.getMetadata()

    def _onHorizontalFOV(self, s):
        # Update current pixelSize and magnification
        self._updatePixelSize()
        self._updateMagnification()

    def updateHorizontalFOV(self):
        with self.parent._acq_progress_lock:
            new_fov = self.parent._device.GetViewField()

        # self._setHorizontalFOV(new_fov * 1e-03)
        self.horizontalFoV.value = new_fov * 1e-03
        # Update current pixelSize and magnification
        self._updatePixelSize()
        self._updateMagnification()

    def _setHorizontalFOV(self, value):
        # Ensure fov odemis field always shows the right value
        # Also useful in case fov value that we try to set is
        # out of range
        with self.parent._acq_progress_lock:
            # FOV to mm to comply with Tescan API
            self.parent._device.SetViewField(value * 1e03)
            cur_fov = self.parent._device.GetViewField() * 1e-03
            value = cur_fov
        return value

    def _updateMagnification(self):

        # it's read-only, so we change it only via _value
        mag = self._hfw_nomag / self.horizontalFoV.value
        self.magnification._value = mag
        self.magnification.notify(mag)

    def _onDwellTime(self, dt):
        pass
#         self.parent._device.ScStopScan()
#         self.parent._device.CancelRecv()

    def _onVoltage(self, volt):
        self.parent._device.HVSetVoltage(volt)
        # Adjust brightness and contrast
        # with self.parent._acq_progress_lock:
        #    self.parent._device.DtAutoSignal(self.parent._detector._channel)

    def _setPower(self, value):
        powers = self.power.choices

        self._power = util.find_closest(value, powers)
        if self._power == 0:
            self.parent._device.HVBeamOff()
        else:
            self.parent._device.HVBeamOn()
        return self._power

    def _setPC(self, value):
        currents = self.probeCurrent.choices

        self._probeCurrent = util.find_closest(value, currents)
        self._indexCurrent = util.index_closest(value, self._list_currents)

        # Set the corresponding current index to Tescan SEM
        self.parent._device.SetPCIndex(self._indexCurrent + 1)
        # Adjust brightness and contrast
        # with self.parent._acq_progress_lock:
        #    self.parent._device.DtAutoSignal(self.parent._detector._channel)

        return self._probeCurrent

    def GetVoltagesRange(self):
        """
        return (list of float): accelerating voltage values ordered by index
        """
        voltages = []
        avs = self.parent._device.HVEnumIndexes()
        vol = re.findall(r'\=(.*?)\n', avs)
        for i in enumerate(vol):
            voltages.append(float(i[1]))
        volt_range = (voltages[0], voltages[-2])
        return volt_range

    def GetProbeCurrents(self):
        """
        return (list of float): probe current values ordered by index
        """
        currents = []
        pcs = self.parent._device.EnumPCIndexes()
        cur = re.findall(r'\=(.*?)\n', pcs)
        for i in enumerate(cur):
            # picoamps to amps
            currents.append(float(i[1]) * 1e-12)
        return currents

    def _onScale(self, s):
        self._updatePixelSize()

    def _updatePixelSize(self):
        """
        Update the pixel size using the scale, HFWNoMag and magnification
        """
        fov = self.horizontalFoV.value

        pxs = (fov / self._shape[0],
               fov / self._shape[1])

        # it's read-only, so we change it only via _value
        self.pixelSize._value = pxs
        self.pixelSize.notify(pxs)

        # If scaled up, the pixels are bigger
        pxs_scaled = (pxs[0] * self.scale.value[0], pxs[1] * self.scale.value[1])
        self.parent._metadata[model.MD_PIXEL_SIZE] = pxs_scaled

    def _setScale(self, value):
        """
        value (1 < float, 1 < float): increase of size between pixels compared to
         the original pixel size. It will adapt the translation and resolution to
         have the same ROI (just different amount of pixels scanned)
        return the actual value used
        """
        prev_scale = self._scale
        self._scale = value

        # adapt resolution so that the ROI stays the same
        change = (prev_scale[0] / self._scale[0],
                  prev_scale[1] / self._scale[1])
        old_resolution = self.resolution.value
        new_resolution = (max(int(round(old_resolution[0] * change[0])), 1),
                          max(int(round(old_resolution[1] * change[1])), 1))
        # no need to update translation, as it's independent of scale and will
        # be checked by setting the resolution.
        self.resolution.value = new_resolution  # will call _setResolution()

        return value

    def _setResolution(self, value):
        """
        value (0<int, 0<int): defines the size of the resolution. If the 
         resolution is not possible, it will pick the most fitting one. It will
         recenter the translation if otherwise it would be out of the whole
         scanned area.
        returns the actual value used
        """
        max_size = (int(self._shape[0] // self._scale[0]),
                    int(self._shape[1] // self._scale[1]))

        # at least one pixel, and at most the whole area
        size = (max(min(value[0], max_size[0]), 1),
                max(min(value[1], max_size[1]), 1))
        self._resolution = size

        # setting the same value means it will recheck the boundaries with the
        # new resolution, and reduce the distance to the center if necessary.
        self.translation.value = self.translation.value
        return size

    def _setTranslation(self, value):
        """
        value (float, float): shift from the center. It will always ensure that
          the whole ROI fits the screen.
        returns actual shift accepted
        """
        # compute the min/max of the shift. It's the same as the margin between
        # the centered ROI and the border, taking into account the scaling.
        max_tran = ((self._shape[0] - self._resolution[0] * self._scale[0]) / 2,
                    (self._shape[1] - self._resolution[1] * self._scale[1]) / 2)

        # between -margin and +margin
        tran = (max(min(value[0], max_tran[0]), -max_tran[0]),
                max(min(value[1], max_tran[1]), -max_tran[1]))
        return tran

    def pixelToPhy(self, px_pos):
        """
        Converts a position in pixels to physical (at the current magnification)
        Note: the convention is that in internal coordinates Y goes down, while
        in physical coordinates, Y goes up.
        px_pos (tuple of 2 floats): position in internal coordinates (pixels)
        returns (tuple of 2 floats): physical position in meters 
        """
        pxs = self.pixelSize.value  # m/px
        phy_pos = (px_pos[0] * pxs[0], -px_pos[1] * pxs[1])  # - to invert Y
        return phy_pos


class Detector(model.Detector):
    """
    This is an extension of model.Detector class. It performs the main functionality 
    of the SEM. It sets up a Dataflow and notifies it every time that an SEM image 
    is captured.
    """
    def __init__(self, name, role, parent, channel, detector, **kwargs):
        """
        channel (0<= int): input channel from which to read
        detector (0<= int): detector index
        """
        # It will set up ._shape and .parent
        model.Detector.__init__(self, name, role, parent=parent, **kwargs)
        self._channel = channel
        self._detector = detector
        self.parent._device.DtSelect(self._channel, self._detector)
        self.parent._device.DtEnable(self._channel, 1, 16)  # 16 bits
        # adjust brightness and contrast
        self.parent._device.DtAutoSignal(self._channel)

        # The shape is just one point, the depth
        self._shape = (2 ** 16,)  # only one point
        self.data = SEMDataFlow(self, parent)

        # Special event to request software unblocking on the scan
        self.softwareTrigger = model.Event()

    @roattribute
    def channel(self):
        return self._channel

    @roattribute
    def detector(self):
        return self._detector


class SEMDataFlow(model.DataFlow):
    """
    This is an extension of model.DataFlow. It receives notifications from the 
    detector component once the SEM output is captured. This is the dataflow to 
    which the SEM acquisition streams subscribe.
    """
    def __init__(self, detector, sem):
        """
        detector (model.Detector): the detector that the dataflow corresponds to
        sem (model.Emitter): the SEM
        """
        model.DataFlow.__init__(self)
        self.component = weakref.ref(detector)
        self._sem = weakref.proxy(sem)

        self._sync_event = None  # event to be synchronised on, or None
        self._evtq = None  # a Queue to store received events (= float, time of the event)
        self._prev_max_discard = self._max_discard

    # start/stop_generate are _never_ called simultaneously (thread-safe)
    def start_generate(self):
        comp = self.component()
        if comp is None:
            # sem/component has been deleted, it's all fine, we'll be GC'd soon
            return

        try:
            self._sem.start_acquire(comp)
        except ReferenceError:
            # sem/component has been deleted, it's all fine, we'll be GC'd soon
            pass

    def stop_generate(self):
        comp = self.component()
        if comp is None:
            # sem/component has been deleted, it's all fine, we'll be GC'd soon
            return

        try:
            self._sem.stop_acquire(comp)
            if self._sync_event:
                self._evtq.put(None)  # in case it was waiting for an event
        except ReferenceError:
            # sem/component has been deleted, it's all fine, we'll be GC'd soon
            pass

    def synchronizedOn(self, event):
        """
        Synchronize the acquisition on the given event. Every time the event is
          triggered, the scanner will start a new acquisition/scan.
          The DataFlow can be synchronized only with one Event at a time.
          However each DataFlow can be synchronized, separately. The scan will
          only start once each active DataFlow has received an event.
        event (model.Event or None): event to synchronize with. Use None to
          disable synchronization.
        """
        if self._sync_event == event:
            return

        if self._sync_event:
            self._sync_event.unsubscribe(self)
            self.max_discard = self._prev_max_discard
            if not event:
                self._evtq.put(None)  # in case it was waiting for this event

        self._sync_event = event
        if self._sync_event:
            # if the df is synchronized, the subscribers probably don't want to
            # skip some data
            self._evtq = Queue.Queue()  # to be sure it's empty
            self._prev_max_discard = self._max_discard
            self.max_discard = 0
            self._sync_event.subscribe(self)

    @oneway
    def onEvent(self):
        """
        Called by the Event when it is triggered
        """
        if not self._evtq.empty():
            logging.warning("Received synchronization event but already %d queued",
                            self._evtq.qsize())

        self._evtq.put(time.time())

    def _waitSync(self):
        """
        Block until the Event on which the dataflow is synchronised has been
          received. If the DataFlow is not synchronised on any event, this
          method immediatly returns
        """
        if self._sync_event:
            self._evtq.get()

class Stage(model.Actuator):
    """
    This is an extension of the model.Actuator class. It provides functions for
    moving the Tescan stage and updating the position. 
    """
    def __init__(self, name, role, parent, **kwargs):
        """
        axes (set of string): names of the axes
        """
        axes_def = {}
        self._position = {}

        rng = [-0.5, 0.5]
        axes_def["x"] = model.Axis(unit="m", range=rng)
        axes_def["y"] = model.Axis(unit="m", range=rng)
        axes_def["z"] = model.Axis(unit="m", range=rng)

        # Demand calibrated stage
        if parent._device.StgIsCalibrated() !=1:
            logging.warning("Stage was not calibrated. We are performing calibration now.")
            parent._device.StgCalibrate()

        # Wait for stage to be stable after calibration
        while parent._device.StgIsBusy() != 0:
            # If the stage is busy (movement is in progress), current position is
            # updated approximately every 500 ms
            time.sleep(0.5)

        x, y, z, rot, tilt = parent._device.StgGetPosition()
        self._position["x"] = -x * 1e-3
        self._position["y"] = -y * 1e-3
        self._position["z"] = -z * 1e-3

        model.Actuator.__init__(self, name, role, parent=parent, axes=axes_def, **kwargs)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

        # RO, as to modify it the client must use .moveRel() or .moveAbs()
        self.position = model.VigilantAttribute(
                                    self._applyInversion(self._position),
                                    unit="m", readonly=True)

    def _updatePosition(self):
        """
        update the position VA
        """
        # it's read-only, so we change it via _value
        self.position._value = self._applyInversion(self._position)
        self.position.notify(self.position.value)

    def _doMove(self, pos):
        """
        move to the position 
        """
        with self.parent._acq_progress_lock:
            # Perform move through Tescan API
            # Position from m to mm and inverted
            self.parent._device.StgMoveTo(-pos["x"] * 1e3,
                                        - pos["y"] * 1e3,
                                        - pos["z"] * 1e3)

            # Wait until move is completed
            while self.parent._device.StgIsBusy():
                time.sleep(0.2)

            # Obtain the finally reached position after move is performed.
            # This is mainly in order to keep the correct position in case the
            # move we tried to perform was greater than the maximum possible
            # one.
            x, y, z, rot, tilt = self.parent._device.StgGetPosition()
            self._position["x"] = -x * 1e-3
            self._position["y"] = -y * 1e-3
            self._position["z"] = -z * 1e-3

            self._updatePosition()

    @isasync
    def moveRel(self, shift):
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)

        shift = self._applyInversion(shift)

        for axis, change in shift.items():
            self._position[axis] += change

        pos = self._position
        return self._executor.submit(self._doMove, pos)

    @isasync
    def moveAbs(self, pos):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)

        for axis, new_pos in pos.items():
            self._position[axis] = new_pos

        pos = self._position
        return self._executor.submit(self._doMove, pos)

    def stop(self, axes=None):
        # Empty the queue for the given axes
        self._executor.cancel()
        logging.warning("Stopping all axes: %s", ", ".join(self.axes))

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None


class EbeamFocus(model.Actuator):
    """
    This is an extension of the model.Actuator class. It provides functions for
    adjusting the ebeam focus by changing the working distance i.e. the distance 
    between the end of the objective and the surface of the observed specimen 
    """
    def __init__(self, name, role, parent, axes, ranges=None, **kwargs):
        assert len(axes) > 0
        if ranges is None:
            ranges = {}

        axes_def = {}
        self._position = {}

        # Just z axis
        a = axes[0]
        # The maximum, obviously, is not 1 meter. We do not actually care
        # about the range since Tescan API will adjust the value set if the
        # required one is out of limits.
        rng = [0, 1]
        axes_def[a] = model.Axis(unit="m", range=rng)

        # start at the centre
        self._position[a] = parent._device.GetWD() * 1e-3

        model.Actuator.__init__(self, name, role, parent=parent, axes=axes_def, **kwargs)

        # RO, as to modify it the client must use .moveRel() or .moveAbs()
        self.position = model.VigilantAttribute(
                                    self._applyInversion(self._position),
                                    unit="m", readonly=True)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

    def _updatePosition(self):
        """
        update the position VA
        """
        # it's read-only, so we change it via _value
        self.position._value = self._applyInversion(self._position)
        self.position.notify(self.position.value)

    def _doMove(self, pos):
        """
        move to the position
        """
        # Perform move through Tescan API
        # Position from m to mm and inverted
        with self.parent._acq_progress_lock:
            self.parent._device.SetWD(self._position["z"] * 1e03)
            # Obtain the finally reached position after move is performed.
            wd = self.parent._device.GetWD()
            self._position["z"] = wd * 1e-3

            self._updatePosition()
        # Changing WD results to change in fov
        self.parent._scanner.updateHorizontalFOV()

    @isasync
    def moveRel(self, shift):
        if not shift:
            return model.InstantaneousFuture()
        self._checkMoveRel(shift)

        shift = self._applyInversion(shift)

        for axis, change in shift.items():
            self._position[axis] += change

        pos = self._position
        return self._executor.submit(self._doMove, pos)

    @isasync
    def moveAbs(self, pos):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        pos = self._applyInversion(pos)

        for axis, new_pos in pos.items():
            self._position[axis] = new_pos

        pos = self._position
        return self._executor.submit(self._doMove, pos)

    def stop(self, axes=None):
        # Empty the queue for the given axes
        self._executor.cancel()
        logging.warning("Stopping all axes: %s", ", ".join(self.axes))

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None


class ChamberView(model.DigitalCamera):
    """
    Represents one chamber camera - chamberscope. Provides video consisted of
    static images sent in regular intervals.
    This implementation is for the Tescan.
    """
    def __init__(self, name, role, parent, **kwargs):
        """
        Initialises the device.
        Raise an exception if the device cannot be opened.
        """
        model.DigitalCamera.__init__(self, name, role, parent=parent, **kwargs)

        # Parameters explanation: Chamber camera enabled on channel 0 (reserved),
        # without zoom applied so we get the maximum size of the image (1),
        # in the maximum fps (5), and compression mode 0 (must be so, according,
        # to Tescan API documentation).
        self.parent._device.CameraEnable(0, 1, 5, 0)
        # Wait for camera to be enabled
        while (self.parent._device.CameraGetStatus(0))[0] != 1:
            time.sleep(0.5)
        # Get a first image to determine the resolution
        width, height, img_str = self.parent._device.FetchCameraImage(0)
        self.parent._device.CameraDisable()
        resolution = (height, width)
        self._shape = resolution + (2 ** 8,)
        self.resolution = model.ResolutionVA(resolution, [(1, 1), (2048, 2048)],
                                            readonly=True)

        self.acquisition_lock = threading.Lock()
        self.acquire_must_stop = threading.Event()
        self.acquire_thread = None

        self.data = ChamberDataFlow(self)

        logging.debug("Camera component ready to use.")

    def Shutdown(self):
        self.parent._device.CameraDisable()

    def GetStatus(self):
        """
        return int: chamber camera status, 0 - off, 1 - on
        """
        with self.parent._acq_progress_lock:
            status = self.parent._device.CameraGetStatus(0)  # channel 0, reserved
        return status[0]

    def start_flow(self, callback):
        """
        Set up the chamber camera and start acquiring images.
        callback (callable (DataArray) no return):
         function called for each image acquired
        """
        with self.parent._acq_progress_lock:
            self.parent._device.CameraEnable(0, 1, 5, 0)

        # if there is a very quick unsubscribe(), subscribe(), the previous
        # thread might still be running
        self.wait_stopped_flow()  # no-op is the thread is not running
        self.acquisition_lock.acquire()

        assert(self.GetStatus() == 1)  # Just to be sure

        target = self._acquire_thread_continuous
        self.acquire_thread = threading.Thread(target=target,
                name="chamber camera acquire flow thread",
                args=(callback,))
        self.acquire_thread.start()

    def req_stop_flow(self):
        """
        Cancel the acquisition of a flow of images: there will not be any notify() after this function
        Note: the thread should be already running
        Note: the thread might still be running for a little while after!
        """
        assert not self.acquire_must_stop.is_set()
        self.acquire_must_stop.set()
        # self.parent._device.CancelRecv()
        with self.parent._acq_progress_lock:
            # self.parent._device.CameraDisable()
            # self.parent._device.ScStopScan()
            self.parent._device.CameraDisable()

    def _acquire_thread_continuous(self, callback):
        """
        The core of the acquisition thread. Runs until acquire_must_stop is set.
        """
        try:
            while not self.acquire_must_stop.is_set():
                with self.parent._acq_progress_lock:
                    width, height, img_str = self.parent._device.FetchCameraImage(0)
                sem_img = numpy.frombuffer(img_str, dtype=numpy.uint8)
                sem_img.shape = (height, width)
                logging.debug("Acquiring chamber image of %s", sem_img.shape)
                array = model.DataArray(sem_img)
                # update resolution
                self.resolution._value = sem_img.shape
                self.resolution.notify(sem_img.shape)
                # first we wait ourselves the typical time (which might be very long)
                # while detecting requests for stop
                # If the Chamber view is just enabled it may take several seconds
                # to get the first image.

                callback(self._transposeDAToUser(array))

        except:
            logging.exception("Failure during acquisition")
        finally:
            self.acquisition_lock.release()
            logging.debug("Acquisition thread closed")
            self.acquire_must_stop.clear()

    def wait_stopped_flow(self):
        """
        Waits until the end acquisition of a flow of images. Calling from the
         acquisition callback is not permitted (it would cause a dead-lock).
        """
        # "if" is to not wait if it's already finished
        if self.acquire_must_stop.is_set():
            self.acquire_thread.join(10)  # 10s timeout for safety
            if self.acquire_thread.isAlive():
                raise OSError("Failed to stop the acquisition thread")
            # ensure it's not set, even if the thread died prematurely
            self.acquire_must_stop.clear()

    def terminate(self):
        """
        Must be called at the end of the usage
        """
        self.Shutdown()


class ChamberDataFlow(model.DataFlow):
    def __init__(self, camera):
        """
        camera: chamber camera instance ready to acquire images
        """
        model.DataFlow.__init__(self)
        self.component = weakref.ref(camera)

    def start_generate(self):
        comp = self.component()
        if comp is None:
            return
        comp.start_flow(self.notify)

    def stop_generate(self):
        comp = self.component()
        if comp is None:
            return
        comp.req_stop_flow()  # FIXME


PRESSURE_VENTED = 1e05  # Pa
PRESSURE_PUMPED = 1e-02  # Pa
VACUUM_TIMEOUT = 5 * 60  # seconds
class ChamberPressure(model.Actuator):
    """
    This is an extension of the model.Actuator class. It provides functions for
    adjusting the chamber pressure. It actually allows the user to evacuate or
    vent the chamber and get the current pressure of it.
    """
    def __init__(self, name, role, parent, ranges=None, **kwargs):
        axes = {"pressure": model.Axis(unit="Pa",
                                       choices={PRESSURE_VENTED: "vented",
                                                PRESSURE_PUMPED: "vacuum"})}
        model.Actuator.__init__(self, name, role, parent=parent, axes=axes, **kwargs)

        # last official position
        if self.GetStatus() == 0:
            self._position = PRESSURE_PUMPED
        else:
            self._position = PRESSURE_VENTED

        # RO, as to modify it the client must use .moveRel() or .moveAbs()
        self.position = model.VigilantAttribute(
                                    {"pressure": self._position},
                                    unit="Pa", readonly=True)
        # Almost the same as position, but gives the current position
        self.pressure = model.VigilantAttribute(self._position,
                                    unit="Pa", readonly=True)

        # will take care of executing axis move asynchronously
        self._executor = CancellableThreadPoolExecutor(max_workers=1)  # one task at a time

    def GetStatus(self):
        """
        return int: vacuum status, 
            -1 error 
            0 ready for operation
            1 pumping in progress
            2 venting in progress
            3 vacuum off (pumps are switched off, valves are closed)
            4 chamber open
        """
        with self.parent._acq_progress_lock:
            status = self.parent._device.VacGetStatus()  # channel 0, reserved
        return status

    def terminate(self):
        if self._executor:
            self.stop()
            self._executor.shutdown()
            self._executor = None

    def _updatePosition(self):
        """
        update the position VA and .pressure VA
        """
        # it's read-only, so we change it via _value
        pos = self.parent._device.VacGetPressure(0)
        self.pressure._value = pos
        self.pressure.notify(pos)

        # .position contains the last known/valid position
        # it's read-only, so we change it via _value
        self.position._value = {"pressure": self._position}
        self.position.notify(self.position.value)

    @isasync
    def moveRel(self, shift):
        self._checkMoveRel(shift)

        # convert into an absolute move
        pos = {}
        for a, v in shift.items:
            pos[a] = self.position.value[a] + v

        return self.moveAbs(pos)

    @isasync
    def moveAbs(self, pos):
        if not pos:
            return model.InstantaneousFuture()
        self._checkMoveAbs(pos)
        return self._executor.submit(self._changePressure, pos["pressure"])

    def _changePressure(self, p):
        """
        Synchronous change of the pressure
        p (float): target pressure
        """
        if p["pressure"] == PRESSURE_VENTED:
            self.parent._device.VacVent()
        else:
            self.parent._device.VacPump()

        start = time.time()
        while not self.GetStatus() == 0:
            if (time.time() - start) >= VACUUM_TIMEOUT:
                raise TimeoutError("Vacuum action timed out")
            # Update chamber pressure until pumping/venting process is done
            self._updatePosition()
        self._position = p
        self._updatePosition()

    def stop(self, axes=None):
        self._executor.cancel()
        logging.warning("Stopped pressure change")


class Light(model.Emitter):
    """
    Chamber illumination LED component.
    """
    def __init__(self, name, role, parent, **kwargs):
        model.Emitter.__init__(self, name, role, parent=parent, **kwargs)

        self._shape = ()
        self.power = model.FloatContinuous(10., {0., 10.}, unit="W")
        # turn on when initializing
        self.parent._device.ChamberLed(1)
        self.power.subscribe(self._updatePower)
        # just one band: white
        # emissions is list of 0 <= floats <= 1. Always 1.0: cannot lower it.
        self.emissions = model.ListVA([1.0], unit="", setter=lambda x: [1.0])
        # TODO: update spectra VA to support the actual spectra of the lamp
        self.spectra = model.ListVA([(380e-9, 390e-9, 560e-9, 730e-9, 740e-9)],
                                    unit="m", readonly=True)
        self._metadata[model.MD_IN_WL] = (380e-9, 740e-9)

    def _updatePower(self, value):
        # Switch the chamber LED based on the power value (On in case of max,
        # off in case of min)
        if value == self.power.range[1]:
            self.parent._device.ChamberLed(1)
        else:
            self.parent._device.ChamberLed(0)
        self._metadata[model.MD_LIGHT_POWER] = self.power.value
