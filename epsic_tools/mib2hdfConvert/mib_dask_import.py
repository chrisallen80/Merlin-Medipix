#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""

This script imports Medipix mib files into numpy.memmap, usese dask.array for
reshaping and reformatting and passes as a lazy signal to hyperspy.
It works on Raw and not-RAW files and can accommodate single chip and
quad chip data.
If the data is from quad chip it adds crosses of zero pixel values.
The code looks into the frame exposure times in the MIB header characters and
finds whether all the frame times are the same or there is a flyback pixel.
This would then create a STEM_flag, resulting to further reshaping of the data.
It only needs the mib file for import, i.e. ignores the .HDR file.
Code can accommodate unfinished 4D-STEM frames as it uses the file size to figure
out the number of frames.


"""
import os
import numpy as np
import dask.array as da
import hyperspy.api as hs
from math import floor
from scipy.signal import find_peaks
import h5py
from struct import unpack

hs.preferences.GUIs.warn_if_guis_are_missing = False
hs.preferences.save()


def _manageHeader(fname):
    """
    Get necessary information from the header of the .mib file.

    Parameters
    ----------
    fname : str
        Filename for MIB file.

    Returns
    -------
    hdr : tuple
        (DataOffset,NChips,PixelDepthInFile,sensorLayout,Timestamp,shuttertime,bitdepth)

    Examples
    --------
    Output for 6bit 256*256 data:
    (768, 4, 'R64', '2x2', '2019-06-14 11:46:12.607836', 0.0002, 6)
    Output for 12bit single frame not RAW:
    (768, 4, 'U16', '2x2', '2019-06-06 11:12:42.001309', 0.001, 12)
    Output for RAW data:
    (768, 4, 'R64', '2x2', '2020-02-04 11:53:32.295336', 0.0007, 1)

    """
    Header = str()
    with open(fname, 'rb') as input:
        aByte = input.read(1)
        Header += str(aByte.decode('ascii'))
        # This gets rid of the header
        while aByte and ord(aByte) != 0:
            aByte = input.read(1)
            Header += str(aByte.decode('ascii'))

    elements_in_header = Header.split(',')

    DataOffset = int(elements_in_header[2])

    NChips = int(elements_in_header[3])

    PixelDepthInFile = elements_in_header[6]
    sensorLayout = elements_in_header[7].strip()
    Timestamp = elements_in_header[9]
    shuttertime = float(elements_in_header[10])

    if PixelDepthInFile == 'R64':
        bitdepth = int(elements_in_header[18])  # RAW
    elif PixelDepthInFile == 'U16':
        bitdepth = 12
    elif PixelDepthInFile == 'U08':
        bitdepth = 6
    elif PixelDepthInFile == 'U32':
        bitdepth = 24

    hdr = (DataOffset, NChips, PixelDepthInFile, sensorLayout, Timestamp, shuttertime, bitdepth)

    return hdr


def parse_hdr(fp):
    """
    Parse information from mib file header info from _manageHeader function.

    Parameters
    ----------
    fp : str
        Filepath to .mib file.

    Returns
    -------
    hdr_info : dict
        Dictionary containing header info extracted from .mib file.
    Example:
    RAW mib data
    {'width': 512,
    'height': 512,
    'Assembly Size': '2x2',
    'offset': 768,
    'data-type': 'unsigned',
    'data-length': '1',
    'Counter Depth (number)': 1,
    'raw': 'R64',
    'byte-order': 'dont-care',
    'record-by': 'image',
    'title': '/dls/e02/data/2020/cm26481-1/Merlin/testing/20200204 115306/test',
    'date': '20200204',
    'time': '11:53:32.295336',
    'data offset': 768}

    """
    hdr_info = {}

    read_hdr = _manageHeader(fp)

    # Set the array size of the chip

    if read_hdr[3] == '1x1':
        hdr_info['width'] = 256
        hdr_info['height'] = 256
    elif read_hdr[3] == '2x2':
        hdr_info['width'] = 512
        hdr_info['height'] = 512

    hdr_info['Assembly Size'] = read_hdr[3]

    # Set mib offset
    hdr_info['offset'] = read_hdr[0]
    # Set data-type
    hdr_info['data-type'] = 'unsigned'
    # Set data-length
    if read_hdr[6] == '1':
        # Binary data recorded as 8 bit numbers
        hdr_info['data-length'] = '8'
    else:
        # Changes 6 to 8 , 12 to 16 and 24 to 32 bit
        cd_int = int(read_hdr[6])
        hdr_info['data-length'] = str(int((cd_int + cd_int / 3)))

    hdr_info['Counter Depth (number)'] = int(read_hdr[6])
    if read_hdr[2] == 'R64':
        hdr_info['raw'] = 'R64'
    else:
        hdr_info['raw'] = 'MIB'
    # Set byte order
    hdr_info['byte-order'] = 'dont-care'
    # Set record by to stack of images
    hdr_info['record-by'] = 'image'

    # Set title to file name
    hdr_info['title'] = fp.split('.')[0]
    # Set time and date
    # Adding the try argument to accommodate the new hdr formatting as of April 2018
    try:
        year, month, day_time = read_hdr[4].split('-')
        day, time = day_time.split(' ')
        hdr_info['date'] = year + month + day
        hdr_info['time'] = time
    except:
        day, month, year_time = read_hdr[4].split('/')
        year, time = year_time.split(' ')
        hdr_info['date'] = year + month + day
        hdr_info['time'] = time

    hdr_info['data offset'] = read_hdr[0]

    return hdr_info


def add_crosses(a):
    """
    Adds 3 pixel buffer cross to quad chip data. the input can be a stack or a reshaped 4DSTEM dask object. It returns
    the object with the same number of axes.

    Parameters
    ----------
    a : dask.array
        Stack of raw frames or reshaped dask array object, prior to dimension reshaping, to insert
        3 pixel buffer cross into.

    Returns
    -------
    b : dask.array
        Stack of frames or reshaped 4DSTEM object including 3 pixel buffer cross in the diffraction plane.
    """
    # Determine dimensions of raw frame data
    a_type = a.dtype
    original_shape = a.shape
    a_shape = a.shape

    len_a_shape = len(a_shape)
    print(len_a_shape)
    if len_a_shape == 4:
        a = a.reshape(a_shape[0] * a_shape[1], a_shape[2], a_shape[3])
        a_shape = a.shape
        len_a_shape = len(a_shape)

    img_axes = len_a_shape - 2, len_a_shape - 1
    a_half = int(a_shape[img_axes[0]] / 2), int(a_shape[img_axes[1]] / 2)
    # Define 3 pixel wide cross of zeros to pad raw data
    z_array = da.zeros((a_shape[0], a_shape[1], 3), dtype=a_type)
    z_array2 = da.zeros((a_shape[0], 3, a_shape[img_axes[1]] + 3), dtype=a_type)
    # Insert blank cross into raw data

    b = da.concatenate((a[:, :, :a_half[1]], z_array, a[:, :, a_half[1]:]), axis=-1)

    b = da.concatenate((b[:, :a_half[0], :], z_array2, b[:, a_half[0]:, :]), axis=-2)

    if len(original_shape) == 4:
        print('reshaping to the original shape')
        b = b.reshape(original_shape[0], original_shape[1], original_shape[2] + 3, original_shape[3] + 3)

    return b


def get_mib_depth(hdr_info, fp):
    """
    Determine the total number of frames based on .mib file size.

    Parameters
    ----------
    hdr_info : dict
        Dictionary containing header info extracted from .mib file.
    fp : filepath
        Path to .mib file.

    Returns
    -------
    depth : int
        Number of frames in the stack
    """
    # Define standard frame sizes for quad and single medipix chips
    if hdr_info['Assembly Size'] == '2x2':
        mib_file_size_dict = {
            '1': 33536,
            '6': 262912,
            '12': 525056,
            '24': 1049344,
        }
    if hdr_info['Assembly Size'] == '1x1':
        mib_file_size_dict = {
            '1': 8576,
            '6': 65920,
            '12': 131456,
            '24': 262528,
        }

    file_size = os.path.getsize(fp[:-3] + 'mib')
    if hdr_info['raw'] == 'R64':

        single_frame = mib_file_size_dict.get(str(hdr_info['Counter Depth (number)']))
        depth = int(file_size / single_frame)
    elif hdr_info['raw'] == 'MIB':
        if hdr_info['Counter Depth (number)'] == '1':
            # 1 bit and 6 bit non-raw frames have the same size
            single_frame = mib_file_size_dict.get('6')
            depth = int(file_size / single_frame)
        else:
            single_frame = mib_file_size_dict.get(str(hdr_info['Counter Depth (number)']))
            depth = int(file_size / single_frame)

    return depth


def mib_to_daskarr(hdr_info, fp, mmap_mode='r'):
    """
    Reads the binary mib file into a numpy memmap object and returns as dask array object
    
    Parameters
    --------------
    hdr_info: dict
        output from parse_hdr function
    fp: str
        MIB file name / path
    mmap_mode: str
        memmpap read mode - default is 'r'
    Returns
    --------------
    data_da: data as a dask array object

    Example
    --------------
    returned object when passed the parameters for a 1 bit 256*256 (65536) stack of frames
    dask.array<array, shape=(2197815296,), dtype=uint8, chunksize=(68681728,), chunktype=numpy.ndarray>
        
    """
    data_length = hdr_info['data-length']
    data_type = hdr_info['data-type']
    endian = hdr_info['byte-order']
    read_offset = 0

    if data_type == 'signed':
        data_type = 'int'
    elif data_type == 'unsigned':
        data_type = 'uint'
    elif data_type == 'float':
        pass
    else:
        raise TypeError('Unknown "data-type" string.')

    # mib data always big-endian
    endian = '>'
    data_type += str(int(data_length))
    # uint1 not a valid dtype
    if data_type == 'uint1':
        data_type = 'uint8'
        data_type = np.dtype(data_type)
    else:
        data_type = np.dtype(data_type)
    data_type = data_type.newbyteorder(endian)

    data_mem = np.memmap(fp,
                         offset=read_offset,
                         dtype=data_type,
                         mode=mmap_mode)
    data_da = da.from_array(data_mem)
    return data_da


def get_hdr_bits(hdr_info):
    """
    gets the number of character bits for the header for each frame given the data type
    Parameters
    ----------
    hdr_info: dict - output of the parse_hdr function

    Returns
    -------
    hdr_bits: int - number of characters in the header

    Example
    -------
    for 1 bit RAW mib data:

    768

    """
    data_length = hdr_info['data-length']
    data_type = hdr_info['data-type']

    if data_type == 'signed':
        data_type = 'int'
    elif data_type == 'unsigned':
        data_type = 'uint'
    elif data_type == 'float':
        pass
    else:
        raise TypeError('Unknown "data-type" string.')

    # mib data always big-endian
    endian = '>'
    data_type += str(int(data_length))
    # uint1 not a valid dtype
    if data_type == 'uint1':
        data_type = 'uint8'
        data_type = np.dtype(data_type)
    else:
        data_type = np.dtype(data_type)
    data_type = data_type.newbyteorder(endian)

    if data_length == '1':
        hdr_multiplier = 1
    else:
        hdr_multiplier = (int(data_length) / 8) ** -1

    hdr_bits = int(hdr_info['data offset'] * hdr_multiplier)

    return hdr_bits


def read_exposures(hdr_info, fp, pct_frames_to_read=0.1):
    """
    Looks into the frame times of the first 10 pct of the frames to see if they are
    all the same (TEM) or there is a more intense flyback (4D-STEM). This works due to the way we trigger the
    4DSTEM acquisitions at ePSIC.
    For this to work, the tick in the Merlin software to print exp time into header
    must be selected!

    Parameters
    -------------
    hdr_info: dict
        Output from parse_hdr function
    fp: str
        MIB file name / path
    pct_frames_to_read : float
        Percentage of frames to read, default value 0.1

    Returns
    ------------
    exp_time: list
        List of frame exposure times in seconds
    """
    width = hdr_info['width']
    height = hdr_info['height']
    depth = get_mib_depth(hdr_info, fp)

    record_by = hdr_info['record-by']

    data = mib_to_daskarr(hdr_info, fp)
    hdr_bits = get_hdr_bits(hdr_info)

    if record_by == 'vector':  # spectral image
        size = (height, width, depth)
        data = data.reshape(size)
    elif record_by == 'image':  # stack of images
        width_height = width * height
        # remove headers at the beginning of each frame and reshape

        if hdr_info['raw'] == 'R64':
            try:
                # the header for the case of 12 bit data should be unpacked first
                if hdr_info['Counter Depth (number)'] == 12:
                    data = data.reshape(-1, width_height + hdr_bits)[:, :68]
                    data_crop = data[:int(depth * pct_frames_to_read)]
                    d = data_crop.compute()
                    exp_time = []
                    for frame in range(d.shape[0]):
                        frame_text = str()
                        for item in d[frame]:
                            temp = unpack('cc', item)
                            c1 = temp[1].decode('ascii')
                            c2 = temp[0].decode('ascii')
                            frame_text = frame_text + c1
                            frame_text = frame_text + c2
                        exp_time.append(float(frame_text[71:79]))
                else:
                    if hdr_info['Counter Depth (number)'] == 1:
                        # RAW 1 bit data: the header bits are written as uint8 but the frames
                        # are binary and need to be unpacked as such.
                        data = data.reshape(-1, width_height / 8 + hdr_bits)[:, 71:79]

                    else:
                        data = data.reshape(-1, width_height + hdr_bits)[:, 71:79]
                    data = data[:, ]
                    data_crop = data[:int(depth * pct_frames_to_read)]
                    d = data_crop.compute()
                    exp_time = []
                    for line in range(d.shape[0]):
                        str_list = [chr(d[line][n]) for n in range(d.shape[1])]
                        exp_time.append(float(''.join(str_list)))
            except ValueError:
                print('Frame exposure times are not appearing in header!')
        # TODO complete the cases for the non-RAW scenarios
        else:
            try:
                data = data.reshape(-1, width_height + hdr_bits)[:, 71:79]
                data = data[:, ]
                data_crop = data[:int(depth * pct_frames_to_read)]
                d = data_crop.compute()
                exp_time = []
                for line in range(d.shape[0]):
                    str_list = [chr(d[line][n]) for n in range(d.shape[1])]
                    exp_time.append(float(''.join(str_list)))
            except ValueError:
                print('Frame exposure times are not appearing in header!')
    return exp_time


def STEM_flag_dict(exp_times_list):
    """
    Determines whether a .mib file contains STEM or TEM data and how many
    frames to skip due to triggering from a list of exposure times.

    Parameters
    ----------
    exp_times_list : list
        List of exposure times extracted from a .mib file.

    Returns
    -------
    output : dict
        Dictionary containing - STEM_flag, scan_X, exposure_time,
                                number_of_frames_to_skip, flyback_times
    Example
    -------
    {'STEM_flag': 1,
     'scan_X': 256,
     'exposure time': 0.0007,
     'number of frames_to_skip': 136,
     'flyback_times': [0.0392, 0.0413, 0.012625, 0.042]}
    """
    output = {}
    times_set = set(exp_times_list)
    # If single exposure times in header, treat as TEM data.
    if len(times_set) == 1:
        output['STEM_flag'] = 0
        output['scan_X'] = None
        output['exposure time'] = list(times_set)
        output['number of frames_to_skip'] = None
        output['flyback_times'] = None
    # In case exp times not appearing in header treat as TEM data
    elif len(times_set) == 0:

        output['STEM_flag'] = 0
        output['scan_X'] = None
        output['exposure time'] = None
        output['number of frames_to_skip'] = None
        output['flyback_times'] = None
    # Otherwise, treat as STEM data.
    else:
        STEM_flag = 1
        # Check that the smallest time is the majority of the values
        exp_time = max(times_set, key=exp_times_list.count)
        if exp_times_list.count(exp_time) < int(0.9 * len(exp_times_list)):
            print('Something wrong with the triggering!')
        peaks = [i for i, e in enumerate(exp_times_list) if e != exp_time]
        # Diff between consecutive elements of the array
        lines = np.ediff1d(peaks)

        if len(set(lines)) == 1:
            scan_X = lines[0]
            frames_to_skip = peaks[0]
        else:
            # Assuming the last element to be the line length
            scan_X = lines[-1]
            check = np.ravel(np.where(lines == scan_X, True, False))
            # Checking line lengths
            start_ind = np.where(check == False)[0][-1] + 2
            frames_to_skip = peaks[start_ind]

        flyback_times = list(times_set)
        flyback_times.remove(exp_time)
        output['STEM_flag'] = STEM_flag
        output['scan_X'] = scan_X
        output['exposure time'] = exp_time
        output['number of frames_to_skip'] = frames_to_skip
        output['flyback_times'] = flyback_times

    return output


def mib_to_h5stack(fp, hdr_info, save_path, mmap_mode='r'):
    """
    Read a .mib file using memory mapping where the array
    is stored on disk and not directly loaded, but may be treated
    like a dask array. It writes the data in chunks into an h5 file.

    Parameters
    ----------
    fp: str
        Filepath of .mib file to be loaded.

    hdr_info: dict
        A dictionary containing the keywords as parsed by read_hdr
    save_path: str, h5 filename path to save the h5 file stack
    mmap_mode: default 'r' - {None, 'r+', 'r', 'w+', 'c'}, optional
        If not None, then memory-map the file, using the given mode
        (see `numpy.memmap`).  The mode has no effect for pickled or
        zipped files.

    Returns
    -------
    None
    """
    width = hdr_info['width']
    height = hdr_info['height']

    record_by = hdr_info['record-by']
    depth = get_mib_depth(hdr_info, fp)

    data = mib_to_daskarr(hdr_info, fp)
    hdr_bits = get_hdr_bits(hdr_info)

    if record_by == 'vector':  # spectral image
        size = (height, width, depth)
        data = data.reshape(size)

    elif record_by == 'image':  # stack of images
        width_height = width * height

        # remove headers at the beginning of each frame and reshape
        if hdr_info['raw'] == 'R64':
            if hdr_info['Assembly Size'] == '2x2':
                if hdr_info['Counter Depth (number)'] == 1:
                    _stack_h5dump(data, hdr_info, save_path, raw_binary=True)
                else:
                    # All the other counter depths RAW format
                    _stack_h5dump(data, hdr_info, save_path)
        # none RAW case - not tested
        # TODO: test this for none RAW files - also single chip data!
        elif hdr_info['raw'] == 'MIB':
            _stack_h5dump(data, hdr_info, save_path)
    return


def _stack_h5dump(data, hdr_info, saving_path, raw_binary=False):
    """
    Incremental reading of a large stack dask array object and saving it in a h5 file.

    Parameters
    ----------
    data: dask array object
    hdr_info: dict, header info parsed by the parse_hdr function
    saving_path: str, h5 file name and path
    raw_binary: default False - Need to be True for binary RAW data

    Returns
    -------
    None
    """
    stack_num = 100
    hdr_bits = get_hdr_bits(hdr_info)
    width = hdr_info['width']
    height = hdr_info['height']
    width_height = width * height
    if raw_binary is True:
        # RAW 1 bit data: the header bits are written as uint8 but the frames
        # are binary and need to be unpacked as such.
        data = data.reshape(-1, int(width_height / 8 + hdr_bits))
    else:
        data = data.reshape(-1, int(width_height + hdr_bits))

    data = data[:, hdr_bits:]
    iters_num = int(data.shape[0] / stack_num) + 1
    for i in range(iters_num):
        if (i + 1) * stack_num < data.shape[0]:
            if i == 0:
                print(i)
                data_dump0 = data[:(i + 1) * stack_num, :]
                print(data_dump0.shape)
                if raw_binary is True:
                    data_dump1 = np.unpackbits(data_dump0)
                    data_dump1.reshape(data_dump0.shape[0], data_dump0.shape[1] * 8)
                    data_dump1 = _untangle_raw(data_dump1, hdr_info, data_dump0.shape[0])
                else:
                    data_dump1 = _untangle_raw(data_dump0, hdr_info, data_dump0.shape[0])

                _h5_chunk_write(data_dump1, saving_path)
                print(data_dump1.shape)
                del data_dump0
                del data_dump1
            else:
                print(i)
                data_dump0 = data[i * stack_num:(i + 1) * stack_num, :]
                print(data_dump0.shape)
                if raw_binary is True:
                    data_dump1 = np.unpackbits(data_dump0)
                    data_dump1.reshape(data_dump0.shape[0], data_dump0.shape[1] * 8)
                    data_dump1 = _untangle_raw(data_dump1, hdr_info, data_dump0.shape[0])
                else:
                    data_dump1 = _untangle_raw(data_dump0, hdr_info, data_dump0.shape[0])
                _h5_chunk_write(data_dump1, saving_path)
                print(data_dump1.shape)
                del data_dump0
                del data_dump1
        else:
            print(i)
            data_dump0 = data[i * stack_num:, :]
            print(data_dump0.shape)
            if raw_binary is True:
                data_dump1 = np.unpackbits(data_dump0)
                data_dump1.reshape(data_dump0.shape[0], data_dump0.shape[1] * 8)
                data_dump1 = _untangle_raw(data_dump1, hdr_info, data_dump0.shape[0])
            else:
                data_dump1 = _untangle_raw(data_dump0, hdr_info, data_dump0.shape[0])
            _h5_chunk_write(data_dump1, saving_path)
            print(data_dump1.shape)
            del data_dump0
            del data_dump1
            return


def _h5_chunk_write(data, saving_path):
    """
    Incremental saving of the data into h5 file
    if the h5 file does not exists, creates it and if it does appends the data to the existing dataset
    h5 dataset key: 'data_stack'

    Parameters
    ----------
    data: dask array object
    saving_path: str, path and name of the h5 file

    Returns
    -------
    None
    """
    if os.path.exists(saving_path):
        with h5py.File(saving_path, 'a') as hf:
            print('appending to existing dataset')
            hf['data_stack'].resize((hf['data_stack'].shape[0] + data.shape[0]), axis=0)
            hf['data_stack'][-data.shape[0]:, :, :] = data
    else:
        hf = h5py.File(saving_path, 'w')
        print('creating the h5 file')
        hf.create_dataset('data_stack', data=data, maxshape=(None, data.shape[1], data.shape[2]), compression='gzip')
    return


def _untangle_raw(data, hdr_info, stack_size):
    """
    Corrects for the tangled raw mib format - Only the case for quad chip is considered here.
    
    Inputs
    --------
        data: dask array object - as stack with the detector array unreshaped, e.g. for a 
        single frame 512*512: (1, 262144)
        hdr_info: dict, with the info read from the header- ouput of the parse_hdr function
        stack_size: The number of frames in the data
        
    Outputs
    ----------
    untangled_data: corrected dask array object reshaped on the detector plane, e.g. for a 
    single frame case as above: (1, 512, 512)
    """
    width = hdr_info['width']
    height = hdr_info['height']
    width_height = width * height
    if hdr_info['Counter Depth (number)'] == 24 or hdr_info['Counter Depth (number)'] == 12:
        cols = 4

    elif hdr_info['Counter Depth (number)'] == 1:
        cols = 64

    elif hdr_info['Counter Depth (number)'] == 6:
        cols = 8

    data = data.reshape((stack_size * width_height))

    data = data.reshape(stack_size, height * (height // cols), cols)

    data = da.flip(data, 2)

    if hdr_info['Assembly Size'] == '2x2':
        data = data.reshape((stack_size * width_height))
        data = data.reshape(stack_size, 512 // 2, 512 * 2)

        det1 = data[:, :, 0:256]
        det2 = data[:, :, 256:512]
        det3 = data[:, :, 512:512 + 256]
        det4 = data[:, :, 512 + 256:]

        det3 = da.flip(det3, 2)
        det3 = da.flip(det3, 1)

        det4 = da.flip(det4, 2)
        det4 = da.flip(det4, 1)

        untangled_data = da.concatenate((da.concatenate((det1, det3), 1), da.concatenate((det2, det4), 1)), 2)
    return untangled_data


def reshape_4DSTEM_SumFrames(data):
    """
    Reshapes the lazy-imported stack of dimensions: (xxxxxx|Det_X, Det_Y) to the correct scan pattern
    shape: (x, y | Det_X, Det_Y) when the frame exposure times are not in header bits.
    It utilises the over-exposed fly-back frame to identify the start of the lines in the first 20
    lines of frames,checks line length consistancy and finds the number of frames to skip at the
    beginning (this number is printed out as string output).
    Parameters
    ----------
    data : hyperspy Signal2D imported mib file with diensions of: framenumbers|Det_X, Det_Y
    Returns
    -------
    data_reshaped : reshaped data (x, y | Det_X, Det_Y)
    skip_ind : number of frames skipped
    """
    # Assuming scan_x, i.e. number of probe positions in a line is square root
    # of total number of frames
    scan_x = int(np.sqrt(data.axes_manager[0].size))
    # detector size in pixels
    det_size = data.axes_manager[1].size
    # crop the first ~20 lines
    data_crop = data.inav[0:20 * scan_x]
    data_crop_t = data_crop.T
    data_crop_t_sum = data_crop_t.sum()
    # summing over patterns
    intensity_array = data_crop_t_sum.data
    intensity_array = intensity_array.compute()
    peaks = find_peaks(intensity_array, distance=scan_x)
    # Difference between consecutive elements of the array
    lines = np.ediff1d(peaks[0])
    # Assuming the last element to be the line length
    line_len = lines[-1]
    if line_len != scan_x:
        raise ValueError('Fly_back does not correspond to correct line length!')
        #  Exits this routine and reshapes using scan size instead
    # Checking line lengths
    check = np.ravel(np.where(lines == line_len, True, False))
    # In case there is a False in there take the index of the last False
    if ~np.all(check):
        start_ind = np.where(check == False)[0][-1] + 2
        # Adding 2 - instead of 1 -  to be sure scan is OK
        skip_ind = peaks[0][start_ind]
    # In case they are all True take the index of the first True
    else:
        # number of frames to skip at the beginning
        skip_ind = peaks[0][0]
    # Number of lines
    n_lines = floor((data.data.shape[0] - skip_ind) / line_len)
    # with the skipped frames removed
    data_skip = data.inav[skip_ind:skip_ind + (n_lines * line_len)]
    data_skip.data = data_skip.data.reshape(n_lines, line_len, det_size, det_size)
    data_skip.axes_manager._axes.insert(0, data_skip.axes_manager[0].copy())
    data_skip.get_dimensions_from_data()
    print('Reshaping using the frame intensity sums of the first 20 lines')
    print('Number of frames skipped at the beginning: ', skip_ind)
    # Croppimg the flyaback pixel at the start
    data_skip = data_skip.inav[1:]
    return data_skip, skip_ind


def reshape_4DSTEM_FlyBack(data):
    """Reshapes the lazy-imported frame stack to navigation dimensions determined
    based on stored exposure times.


    Parameters
    ----------
    data : hyperspy lazy Signal2D
        Lazy loaded electron diffraction data: <framenumbers | det_size, det_size>
        the data metadata contains flyback info as:
            ├── General
        │   └── title =
        └── Signal
            ├── binned = False
            ├── exposure_time = 0.001
            ├── flyback_times = [0.01826, 0.066, 0.065]
            ├── frames_number_skipped = 68
            ├── scan_X = 256
            └── signal_type = STEM

    Returns
    -------
    data_skip :
        Reshaped electron diffraction data <scan_x, scan_y | det_size, det_size>
    """
    # Get detector size in pixels

    det_size = data.axes_manager[1].size  # detector size in pixels
    # Read metadata
    skip_ind = data.metadata.Signal.frames_number_skipped
    line_len = data.metadata.Signal.scan_X

    n_lines = floor((data.data.shape[0] - skip_ind) / line_len)
    print(n_lines)

    # Remove skipped frames
    data_skip = data.inav[skip_ind:skip_ind + (n_lines * line_len)]
    print(data_skip.data.shape)
    # Reshape signal
    data_skip.data = data_skip.data.reshape(n_lines, line_len, det_size, det_size)
    data_skip.axes_manager._axes.insert(0, data_skip.axes_manager[0].copy())
    data_skip.get_dimensions_from_data()
    # Cropping the bright fly-back pixel
    data_skip = data_skip.inav[1:]

    return data_skip


def h5stack_to_hs(h5_path, hdr_info):
    """
    this function reads the saved stack h5 file into a reshaped 4DSTEM hyerspy lazy object
    chunks are defined as (1000, det_x, det_y)
    This function assumes the mib file path corresponding to this h5 file is the path provided in the hdr_info
    TODO: Make this an argument to input the mib path if different

    Parameters
    ----------
    h5_path: str, full path and name of the h5 stack file
    hdr_info: dict, header info - output from parse_hdr function

    Returns
    -------
    data_hs: lazy Signal2D reshaped object
    """
    f = h5py.File(h5_path, 'r')

    data = f['data_stack']

    chunks = (100, hdr_info['width'], hdr_info['height'])

    x = da.from_array(data, chunks=chunks)
    data_hs = hs.signals.Signal2D(x).as_lazy()

    mib_path = hdr_info['title'] + '.mib'

    exp_times_list = read_exposures(hdr_info, mib_path)
    data_dict = STEM_flag_dict(exp_times_list)

    # Tranferring dict info to metadata
    if data_dict['STEM_flag'] == 1:
        data_hs.metadata.Signal.signal_type = 'STEM'
    else:
        data_hs.metadata.Signal.signal_type = 'TEM'
    data_hs.metadata.Signal.scan_X = data_dict['scan_X']
    data_hs.metadata.Signal.exposure_time = data_dict['exposure time']
    data_hs.metadata.Signal.frames_number_skipped = data_dict['number of frames_to_skip']
    data_hs.metadata.Signal.flyback_times = data_dict['flyback_times']

    if data_hs.metadata.Signal.signal_type == 'TEM' and data_hs.metadata.Signal.exposure_time != None:
        print('This mib file appears to be TEM data. The stack is returned with no reshaping.')

    # to catch single frames:
    if data_hs.axes_manager[0].size == 1:
        print('This mib file is a single frame.')

    try:
        # If the exposure time info not appearing in the header bits use reshape_4DSTEM_SumFrames
        # to reshape otherwise use reshape_4DSTEM_FlyBack function
        if data_hs.metadata.Signal.exposure_time is None:
            (data_hs, skip_ind) = reshape_4DSTEM_SumFrames(data_hs)
            data_hs.metadata.Signal.signal_type = 'STEM'
            data_hs.metadata.Signal.frames_number_skipped = skip_ind
        else:
            print('reshaping using flyback pixel')
            data_hs = reshape_4DSTEM_FlyBack(data_hs)
    except TypeError:
        print(
            'Warning: Reshaping did not work or TEM data with no exposure info. Returning the stack with no reshaping!')
    except ValueError:
        print(
            'Warning: Reshaping did not work or TEM data with no exposure info. Returning the stack with no reshaping!')
    return data_hs


def mib_dask_reader(mib_filename, h5_stack_path=None):
    """Read a .mib file using dask and return as a lazy pyXem / hyperspy signal.

    Parameters
    ----------
    mib_filename : str
        The name of the .mib file to be read.
    h5_stack_path: str, default None
        this is the h5 file path that we can read the data from in the case of large scan arrays
    Returns
    -------
    data_hs : reshaped hyperspy.signals.Signal2D
    If the data is detected to be STEM is reshaped using two functions, one using the 
    exposure times appearing on the header and if no exposure times available using the 
    sum frames and detecting the flyback frames. If TEM data, a single frame or if 
    reshaping the STEM fails, the stack is returned.
                The metadata adds the following domains:
                General
                │   └── title =
                └── Signal
                    ├── binned = False
                    ├── exposure_time = 0.001
                    ├── flyback_times = [0.066, 0.071, 0.065, 0.017825]
                    ├── frames_number_skipped = 90
                    ├── scan_X = 256
                    └── signal_type = STEM

    """
    hdr_stuff = parse_hdr(mib_filename)
    width = hdr_stuff['width']
    height = hdr_stuff['height']
    width_height = width * height
    if h5_stack_path is None:
        data = mib_to_daskarr(hdr_stuff, mib_filename)
        depth = get_mib_depth(hdr_stuff, mib_filename)
        hdr_bits = get_hdr_bits(hdr_stuff)
        if hdr_stuff['Counter Depth (number)'] == 1:
            # RAW 1 bit data: the header bits are written as uint8 but the frames
            # are binary and need to be unpacked as such.
            data = data.reshape(-1, int(width_height / 8 + hdr_bits))
            data = data[:, hdr_bits:]
            # get the shape axis 1 before unpackbit
            s0 = data.shape[0]
            s1 = data.shape[1]
            data = np.unpackbits(data)
            data.reshape(s0, s1 * 8)
        else:
            data = data.reshape(-1, int(width_height + hdr_bits))
            data = data[:, hdr_bits:]
        if hdr_stuff['raw'] == 'R64':
            data = _untangle_raw(data, hdr_stuff, depth)
        elif hdr_stuff['raw'] == 'MIB':
            data = data.reshape(depth, width, height)
    else:
        data = h5stack_to_hs(h5_stack_path, hdr_stuff)
        data = data.data

    exp_times_list = read_exposures(hdr_stuff, mib_filename)
    data_dict = STEM_flag_dict(exp_times_list)

    if hdr_stuff['Assembly Size'] == '2x2':
        # add_crosses expects a dask array object
        data = add_crosses(data)

    data_hs = hs.signals.Signal2D(data).as_lazy()

    # Tranferring dict info to metadata
    if data_dict['STEM_flag'] == 1:
        data_hs.metadata.Signal.signal_type = 'STEM'
    else:
        data_hs.metadata.Signal.signal_type = 'TEM'
    data_hs.metadata.Signal.scan_X = data_dict['scan_X']
    data_hs.metadata.Signal.exposure_time = data_dict['exposure time']
    data_hs.metadata.Signal.frames_number_skipped = data_dict['number of frames_to_skip']
    data_hs.metadata.Signal.flyback_times = data_dict['flyback_times']
    print(data_hs)

    # only attempt reshaping if it is not already reshaped!

    if len(data_hs.data.shape) == 3:
        try:
            if data_hs.metadata.Signal.signal_type == 'TEM':
                print('This mib file appears to be TEM data. The stack is returned with no reshaping.')
                return data_hs
            # to catch single frames:
            if data_hs.axes_manager[0].size == 1:
                print('This mib file is a single frame.')
                return data_hs
            # If the exposure time info not appearing in the header bits use reshape_4DSTEM_SumFrames
            # to reshape otherwise use reshape_4DSTEM_FlyBack function
            if data_hs.metadata.Signal.signal_type == 'STEM' and data_hs.metadata.Signal.exposure_time is None:
                print('reshaping using sum frames intensity')
                (data_hs, skip_ind) = reshape_4DSTEM_SumFrames(data_hs)
                data_hs.metadata.Signal.signal_type = 'STEM'
                data_hs.metadata.Signal.frames_number_skipped = skip_ind
            else:
                print('reshaping using flyback pixel')
                data_hs = reshape_4DSTEM_FlyBack(data_hs)
        except TypeError:
            print(
                'Warning: Reshaping did not work or TEM data with no exposure info. Returning the stack with no reshaping!')
            return data_hs
        except ValueError:
            print(
                'Warning: Reshaping did not work or TEM data with no exposure info. Returning the stack with no reshaping!')
            return data_hs
    return data_hs
