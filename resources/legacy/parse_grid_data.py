import numpy as np
from crcmod import mkCrcFun
import os
import xml.etree.ElementTree as ET
import re
import h5py
from datetime import datetime,timedelta
from parity_check import crc16_xmodem_nd, bcc_nd
import pandas as pd


def find_pattern_in_buffer(buffer, pattern):
    Pattern = re.compile(pattern, re.S)
    return np.array([(ip.start(), ip.end()) for ip in Pattern.finditer(buffer)]).astype(np.int_)

def parse_grid_data_new(file_name, xml_file=None, data_tag='wf_packet', 
        multi_evt=None, multi_step=None, endian='MSB', crc_check=True, data=None,
        skip_et=[],packet_len=None):
    if xml_file is None:
        xml_file = os.path.join(os.path.dirname(__file__),'grid_packet.xml')
    et_packet = ET.parse(xml_file).getroot().find(data_tag)
    '''
    data_tag:
        wf_packet
        ft_packet
        tl_packet
        hk_05b_packet (not all fields are checked)
        (hk_7020_packet (not all fields are checked))
        hk_06b_packet
        hk_08b_packet
        iv_packet (LSB)
        vbr_packet (LSB)
        cg_packet
        hk_grid1x_packet
    '''
    print('========> parsing file ',file_name)
    packet_len = int(et_packet.attrib['packet_len']) if packet_len is None else packet_len
    head,tail = [],[]
    head = [int(v[2:],base=16) for v in et_packet.attrib['head'].split(';')]
    head = head[::-1] if endian=='LSB' else head
    if 'tail' in et_packet.attrib:
        tail = [int(v[2:],base=16) for v in et_packet.attrib['tail'].split(';')]
    tail = tail[::-1] if endian=='LSB' else tail
    match_num = packet_len - len(head) - len(tail)
    pattern = re.escape(bytes(head)) + b'.{' + bytes(f'{match_num}',encoding='utf-8') + b'}'
    if tail:
        pattern += re.escape(bytes(tail))
    #else:
    #    pattern += re.escape(bytes(head))

    #with open(file_name,'rb') as fin:
    #    buffer = fin.read()
    #    index = find_pattern_in_buffer(buffer, pattern)
    buffer = np.fromfile(file_name,dtype=np.uint8) if data is None else data
    index = find_pattern_in_buffer(buffer, pattern)
    if not tail:
        index = np.array([(v[0],v[0]+packet_len) for v in index])

    if xml_file is None:
        xml_file = os.path.join(os.path.dirname(__file__),'grid_packet.xml')
    
    # make low configuration computer can handle large file

    if (index.shape[0]>2e6) & (packet_len>400):
        step = 2000000
        p0,p1 = [],[]
        for i in range(np.ceil(index.shape[0]/step).astype(int)):
            tmp = parse_grid_data_single(file_name=file_name, evt_index=index[step*i:step*(i+1)], xml_file=xml_file, 
                                      data_tag=data_tag, multi_evt=multi_evt, multi_step=multi_step,
                                      endian=endian, crc_check=crc_check,data_array=data,skip_et=skip_et)
            p0.append(tmp[0])
            p1.append(tmp[1])
        q0 = p0[0].copy()
        for k in q0.keys():
            for p in p0[1:]:
                q0[k] = np.r_[q0[k],p[k]]
        q1 = p1[0].copy()
        for k in q1.keys():
            for p in p1[1:]:
                q1[k] = np.r_[q1[k],p[k]]
        return (q0,index)
    else:
        return (parse_grid_data_single(file_name=file_name, evt_index=index, xml_file=xml_file, 
                                      data_tag=data_tag, multi_evt=multi_evt, multi_step=multi_step,
                                      endian=endian, crc_check=crc_check,data_array=data,
                                      skip_et=skip_et,packet_len=packet_len)[0],index)

def parse_grid_data_single(file_name, evt_index, xml_file='grid_packet.xml', data_tag='wf_packet', 
        multi_evt=None, multi_step=None, endian='MSB', crc_check=False, bcc_check=True,data_array=None,
        skip_et=[],packet_len=None):
    if data_tag == 'ft_packet':
        multi_evt = 20 if multi_evt is None else multi_evt
        multi_step = 24 if multi_step is None else multi_step
    if data_tag == 'tl_packet':
        multi_evt = 15 if multi_evt is None else multi_evt
        multi_step = 16 if multi_step is None else multi_step
    if data_tag == 'grid1x_ft_packet':
        multi_evt = 40 if multi_evt is None else multi_evt
        multi_step = 12 if multi_step is None else multi_step
    if multi_evt is None:
        multi_evt = 1

    et_packet = ET.parse(xml_file).getroot().find(data_tag)
    packet_len = int(et_packet.attrib['packet_len']) if packet_len is None else packet_len

    data0 = np.fromfile(file_name,'uint8') if data_array is None else data_array

    data_index = np.r_[[np.arange(v[0],v[1]) for v in evt_index ]].astype(np.int_)
    data0 = data0[data_index].reshape(-1,packet_len)
    packet_num = data0.shape[0]

    data_byte = {}
    data = {}
    # print(data_index.shape)
    tag_info = pd.DataFrame({'name':[], 'start':[], 'size':[], 'len':[]})
    for et in et_packet.findall('./'):
        name = et.tag
        # print(name)
        if name in skip_et:
            continue
        start = int(et.find('start').text)
        if 'vary_wf' in et.find('start').attrib:
            start = (start + int(tag_info[tag_info['name']=='waveform_data'].iloc[0]['start']) + 
                int(tag_info[tag_info['name']=='waveform_data'].iloc[0]['size']) * int(tag_info[tag_info['name']=='waveform_data'].iloc[0]['len']))
        if 'vary_repeat' in et.find('start').attrib:
            print(et.find('start').attrib)
            start = (start + int(et.find('start').attrib['base_start']) + multi_evt*multi_step)
        size = int(et.find('size').text)
        length = int(et.find('len').text)
        if name == 'waveform_data':
            length = data['sample_length'][0]
        tag_info.loc[len(tag_info.index)] = [name, start, size, length]

        endian_bak = endian
        if 'endian' in et.attrib:
            endian_bak,endian = endian,et.attrib['endian']

        if 'repeat' in et.attrib:
            # print(start, multi_step, multi_evt, length, size)
            index = np.r_[[np.arange(start+i*multi_step,start+i*multi_step+length*size) for i in range(multi_evt)]]
            multi_dim = multi_evt
        else:
            index = np.arange(start,start+length*size)
            multi_dim = 1
        print(name)
        data_byte[name] = data0[:,index].reshape(-1,multi_dim,length,size) 

        if 'multi' in et.attrib:
            data_byte[name] = np.repeat(data_byte[name],multi_evt,axis=1)

        data_byte[name] = data_byte[name].reshape(-1,length,size)
        data[name] = byte2int(data_byte[name],endian=endian)
        endian = endian_bak

        if 'incre' in et.attrib:
            data[name] = data[name].reshape(-1,multi_evt,length)
            data[name] = (data[name] + np.tile(np.arange(multi_evt),(data[name].shape[0],1))[...,np.newaxis]).reshape(-1,length).squeeze()

        if (name == 'CRC') & (crc_check):
            data_crc = data[name].reshape(packet_num,multi_evt)
            data['crc_check'] = np.zeros((packet_num,multi_evt),dtype=np.bool_)
            skip0 = int(et.attrib['skip0'])
            skip1 = int(et.attrib['skip1'])
            for j in range(multi_dim):
                if (j==0): 
                    crc_index = np.arange(skip0,start-skip1).astype(int)
                else:
                    crc_index = np.r_[crc_index,np.arange(start+j*multi_step-(multi_step-size),start+j*multi_step-skip1)].astype(int)
                #for i in range(packet_num):
                #    data['crc_check'][i,j] = (crc16_xmodem(data0[i,crc_index]) == data_crc[i,j])
                data['crc_check'][:,j] = (crc16_xmodem_nd(data0[:,crc_index]) == data_crc[:,j])
            if (multi_dim==1) & (multi_evt>1):
                for j in range(1,multi_evt):
                    data['crc_check'][:,j] = data['crc_check'][:,0]
            data['crc_check'] = data['crc_check'].flatten()
        if(name=='bcc') & (bcc_check):
            data_bcc = data[name].reshape(packet_num,multi_evt)
            data['bcc_check'] = np.zeros((packet_num,multi_evt),dtype=np.bool_)
            skip0 = int(et.attrib['skip0'])
            skip1 = int(et.attrib['skip1'])
            for j in range(multi_evt):
                if j==0: 
                    bcc_index = np.arange(skip0,start-skip1).astype(int)
                else:
                    bcc_index = np.r_[bcc_index,np.arange(start+j*multi_step-(multi_step-size),start+j*multi_step-skip1)].astype(int)
                data['bcc_check'][:,j] = (bcc_nd(data0[:,bcc_index]) == data_bcc[:,j])
            data['bcc_check'] = data['bcc_check'].flatten()

    print(tag_info)
    return (data,data_byte)

def byte2int(data,endian='MSB'):
    if endian == 'MSB':
        data = data[...,::-1]
    sp = data.shape
    if sp[-1] <= 4:
        return (data @ 2**(8*np.arange(sp[-1], dtype=np.uint32))).squeeze()
    elif sp[-1] <= 8:
        return (data @ 2**(8*np.arange(sp[-1], dtype=np.uint64))).squeeze()
    else:
        return (data @ 2**(8*np.arange(sp[-1], dtype=object))).squezze()
    pass

if __file__ == "__main__":
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    import tkinter as tk
    import tkinter.filedialog as tkf
    from addict import Dict
    
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost',1)
    #data0, data1,_,_ = parse_grid_data(tkf.askopenfilename(),'grid_packet.xml')
    p1,_ = [Dict(v) for v in parse_grid_data_new(tkf.askopenfilename(),data_tag='ft_packet',endian='MSB')]

## deprecated
def find_event_index(buffer, featureEventNum=20, sampleLen=256):
    LenFeature = featureEventNum * 24 + 8
    pattern_wf = b'\x5a\x5a\x99\x66\x99\x66\x5a\x5a.{' + bytes(f'{LenFeature}', encoding='utf-8') + b'}\xaa\xaa\x99\x66\x99\x66\xaa\xaa'
    feature = find_pattern_in_buffer(buffer, pattern_wf)

    LenWaveform = sampleLen * 2 + 32
    pattern_wf = b'\x5a\x5a\x5a\x5a\x5a\x5a\x5a\x5a.{' + bytes(f'{LenWaveform}', encoding='utf-8') + b'}\xaa\xaa\xaa\xaa\xaa\xaa\xaa\xaa'
    waveform = find_pattern_in_buffer(buffer, pattern_wf)

    print(f"found {len(waveform)} waveforms, found {len(feature)} features")


    if len(buffer) != len(feature)*(LenFeature+16) + len(waveform)*(LenWaveform+16):
        print(f"{len(buffer) - len(feature)*(LenFeature+16) - len(waveform)*(LenWaveform+16)} bytes are unable to decode")
    return feature, waveform
## deprecated
def parse_grid_data(file_name, xml_file=None,multi_evt=20,multi_step=24,endian='MSB',crc_check=False):
    with open(file_name, 'rb') as fin:
        buffer = fin.read()
    feature, waveform = find_event_index(buffer)

    if xml_file is None:
        xml_file = os.path.join(os.path.dirname(__file__),'grid_packet.xml')
    wf_data,wf_data_byte = parse_grid_data_single(file_name, waveform, xml_file=xml_file,
        data_tag='wf_packet',multi_evt=1,multi_step=multi_step,endian=endian,crc_check=crc_check)

    ft_data,ft_data_byte = parse_grid_data_single(file_name, feature, xml_file=xml_file,
        data_tag='ft_packet',multi_evt=multi_evt,multi_step=multi_step,endian=endian,crc_check=crc_check)

    return (wf_data,ft_data,wf_data_byte,ft_data_byte)

## deprecated
def crc16_xmodem(s):
    crc16 = mkCrcFun(0x11021, rev=False, initCrc=0x0000, xorOut=0x0000)
    return crc16(s)

def dict_to_hdf5(fh,data):
    for key,value in data.items():
        if key == 'Header':
            pass
        elif type(value) is dict:
            dict_to_hdf5(fh,data=value)
        else:
            # TODO(liping): temporary treat
            if key == 'DDR_crc':
                dt = h5py.string_dtype(encoding='utf-8')
                fh.create_dataset(key,dtype=dt,data=str(value))
            else:
                fh.create_dataset(key,data=value)

def save_hdf5(path=None,data=None):
    with h5py.File(path+'.hdf5','w') as fh:
        dict_to_hdf5(fh,data)